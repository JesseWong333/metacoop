# -*- coding: utf-8 -*-
# Author: Yifan Lu <yifan_lu@sjtu.edu.cn>
# License: TDG-Attribution-NonCommercial-NoDistrib

import argparse
import os
import time
from typing import OrderedDict
import importlib
import torch
import open3d as o3d
from torch.utils.data import DataLoader, Subset
import numpy as np
import opencood.hypes_yaml.yaml_utils as yaml_utils
from opencood.tools import train_utils, inference_utils
from opencood.data_utils.datasets import build_dataset
from opencood.utils import eval_utils
from PIL import Image
from opencood.visualization import vis_utils, my_vis, simple_vis
torch.multiprocessing.set_sharing_strategy('file_system')

def test_parser():
    parser = argparse.ArgumentParser(description="synthetic data generation")
    parser.add_argument('--model_dir', type=str, required=True,
                        help='Continued training path')
    parser.add_argument('--fusion_method', type=str,
                        default='intermediate',
                        help='no, no_w_uncertainty, late, early or intermediate')
    parser.add_argument('--save_vis_interval', type=int, default=40,
                        help='interval of saving visualization')
    parser.add_argument('--save_npy', action='store_true',
                        help='whether to save prediction and gt result'
                             'in npy file')
    parser.add_argument('--no_score', action='store_true',
                        help="whether print the score of prediction")
    parser.add_argument('--note', default="", type=str, help="any other thing?")
    opt = parser.parse_args()
    return opt

def get_batch_iou(preds, binimgs):
    """Assumes preds has NOT been sigmoided yet
    """
    pred = (preds > 0)
    tgt = binimgs.bool()
    intersect = (pred & tgt).sum().float().item()
    union = (pred | tgt).sum().float().item()

    return intersect, union, intersect / union if (union > 0) else 1.0

def main():
    opt = test_parser()

    assert opt.fusion_method in ['late', 'early', 'intermediate', 'no', 'no_w_uncertainty', 'single', 'single_w_id'] 

    hypes = yaml_utils.load_yaml(None, opt)

    # if 'heter' in hypes:
    #     x_min, x_max = -140.8, 140.8
    #     y_min, y_max = -40, 40
    #     opt.note += f"_{x_max}_{y_max}"
    #     hypes['fusion']['args']['grid_conf']['xbound'] = [x_min, x_max, hypes['fusion']['args']['grid_conf']['xbound'][2]]
    #     hypes['fusion']['args']['grid_conf']['ybound'] = [y_min, y_max, hypes['fusion']['args']['grid_conf']['ybound'][2]]
    #     hypes['model']['args']['grid_conf'] = hypes['fusion']['args']['grid_conf']

    #     new_cav_range = [x_min, y_min, hypes['postprocess']['anchor_args']['cav_lidar_range'][2], \
    #                         x_max, y_max, hypes['postprocess']['anchor_args']['cav_lidar_range'][5]]
        
    #     hypes['preprocess']['cav_lidar_range'] =  new_cav_range
    #     hypes['postprocess']['anchor_args']['cav_lidar_range'] = new_cav_range
    #     hypes['postprocess']['gt_range'] = new_cav_range
    #     hypes['model']['args']['lidar_args']['lidar_range'] = new_cav_range
    #     if 'camera_mask_args' in hypes['model']['args']:
    #         hypes['model']['args']['camera_mask_args']['cav_lidar_range'] = new_cav_range

    #     # reload anchor
    #     yaml_utils_lib = importlib.import_module("opencood.hypes_yaml.yaml_utils")
    #     for name, func in yaml_utils_lib.__dict__.items():
    #         if name == hypes["yaml_parser"]:
    #             parser_func = func
    #     hypes = parser_func(hypes)

    
    # hypes['validate_dir'] = hypes['test_dir']
    if "OPV2V" in hypes['test_dir'] or "v2xsim" in hypes['test_dir']:
        assert "test" in hypes['validate_dir']
    
    # This is used in visualization
    # left hand: OPV2V, V2XSet
    # right hand: V2X-Sim 2.0 and DAIR-V2X
    # left_hand = True if ("OPV2V" in hypes['test_dir'] or "V2XSET" in hypes['test_dir']) else False
    left_hand = True

    print(f"Left hand visualizing: {left_hand}")

    if 'box_align' in hypes.keys():
        hypes['box_align']['val_result'] = hypes['box_align']['test_result']

    print('Creating Model')
    model = train_utils.create_model(hypes)
    # we assume gpu is necessary
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    print('Loading Model from checkpoint')
    saved_path = opt.model_dir
    resume_epoch, model = train_utils.load_saved_model(saved_path, model)
    print(f"resume from {resume_epoch} epoch.")
    opt.note += f"_epoch{resume_epoch}"
    
    if torch.cuda.is_available():
        model.cuda()
    model.eval()

    # setting noise
    np.random.seed(303)
    
    # build dataset for each noise setting
    print('Dataset Building')
    opencood_dataset = build_dataset(hypes, visualize=True, train=False)
    data_loader = DataLoader(opencood_dataset,
                            batch_size=1,
                            num_workers=4,
                            collate_fn=opencood_dataset.collate_batch_test,
                            shuffle=False,
                            pin_memory=False,
                            drop_last=False)
    
    # Create the dictionary for evaluation
    result_stat = {0.3: {'tp': [], 'fp': [], 'gt': 0, 'score': []},                
                0.5: {'tp': [], 'fp': [], 'gt': 0, 'score': []},                
                0.7: {'tp': [], 'fp': [], 'gt': 0, 'score': []}}

    
    infer_info = opt.fusion_method + opt.note

    total_intersect = 0.0
    total_union = 0
    for i, batch_data in enumerate(data_loader):
        print(f"{infer_info}_{i}")
        if batch_data is None:
            continue
        with torch.no_grad():
            batch_data = train_utils.to_device(batch_data, device)

            output = model(batch_data)
            
            seg_preds = output['seg_preds'] # b, 2, h, w
            seg_preds = torch.argmax(seg_preds, dim=1) # b, h, w
            gt_seg = batch_data[hypes['train_agent_ID']]['label_dict_single']['object_seg_label'] # b, h, w
            intersect, union, _ = get_batch_iou(seg_preds, gt_seg)
            total_intersect += intersect
            total_union += union
            
            seg_preds = seg_preds.cpu().squeeze().numpy().astype(np.uint8) # b, h, w
            gt_seg = batch_data[hypes['train_agent_ID']]['label_dict_single']['object_seg_label'].cpu().squeeze().numpy()  # b, h, w

            seg_preds_img = Image.fromarray(seg_preds*255)
            gt_preds_img = Image.fromarray(gt_seg*255)
            seg_preds_img.save(os.path.join('./tmp/', str(i) + '_pred.png'))
            gt_preds_img.save(os.path.join('./tmp/', str(i) + '_gt.png'))
            
    print("iou:{}".format(total_intersect / total_union))

if __name__ == '__main__':
    main()

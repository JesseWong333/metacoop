# -*- coding: utf-8 -*-
# Author: Yifan Lu <yifan_lu@sjtu.edu.cn>, Runsheng Xu <rxx3386@ucla.edu>
# License: TDG-Attribution-NonCommercial-NoDistrib

import argparse
import os
import statistics

import torch
from torch.utils.data import DataLoader, Subset
from tensorboardX import SummaryWriter

import opencood.hypes_yaml.yaml_utils as yaml_utils
from opencood.tools import train_utils
from opencood.data_utils.datasets import build_dataset
import glob
import loralib as lora
from icecream import ic

def train_parser():
    parser = argparse.ArgumentParser(description="synthetic data generation")
    parser.add_argument("--hypes_yaml", "-y", type=str, required=True,
                        help='data generation yaml file needed ')
    parser.add_argument('--model_dir', default='',
                        help='Continued training path')
    parser.add_argument('--fusion_method', '-f', default="intermediate",
                        help='passed to inference.')
    opt = parser.parse_args()
    return opt


def main():
    opt = train_parser()
    hypes = yaml_utils.load_yaml(opt.hypes_yaml, opt)

    print('Dataset Building')
    opencood_train_dataset = build_dataset(hypes, visualize=False, train=True)
    opencood_validate_dataset = build_dataset(hypes,
                                              visualize=False,
                                              train=False)

    train_loader = DataLoader(opencood_train_dataset,
                              batch_size=hypes['train_params']['batch_size'],
                              num_workers=4,
                              collate_fn=opencood_train_dataset.collate_batch_train,
                              shuffle=True,
                              pin_memory=True,
                              drop_last=True,
                              prefetch_factor=2)
    val_loader = DataLoader(opencood_validate_dataset,
                            batch_size=hypes['train_params']['batch_size'],
                            num_workers=4,
                            collate_fn=opencood_train_dataset.collate_batch_train,
                            shuffle=True,
                            pin_memory=True,
                            drop_last=True,
                            prefetch_factor=2)

    print('Creating Model')
    model = train_utils.create_model(hypes)
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    
    # load one side parameters
    if hypes['train_agent_ID'] == -1:
        ego_model_dict = torch.load(hypes['method_ego_path'])
        i_model_dict = torch.load(hypes['method_i_path'])
        ego_model_dict.update(i_model_dict)
        load_results = model.load_state_dict(ego_model_dict, strict=False)
        print("load unexpected_keys:" + str(load_results.unexpected_keys))
        
        extra_agent_name = hypes['model']['args']['defor_encoder_fusion']['agent_names'][1]
        print("tuning parameters:")
        for name, value in model.named_parameters():
            # only tune Lora and paeameters assiaated with agent_names
            if 'lora_' in name or extra_agent_name in name:
                value.requires_grad = True
                print(name)
            else:
                value.requires_grad = False
        # setup optimizer
        params = filter(lambda p: p.requires_grad, model.parameters())
        optimizer = train_utils.setup_optimizer(hypes, params)

    elif hypes['train_agent_ID'] == -3:
        # -3
        model_dict = torch.load(hypes['model_fusion_path'])
        load_results = model.load_state_dict(model_dict, strict=False)
        print("unexpected_keys:" + str(load_results.unexpected_keys))
        print("missing_keys:" + str(load_results.missing_keys))

        model_dict = torch.load(hypes['method_i_path_family'])
        load_results = model.load_state_dict(model_dict, strict=False)
        print("load unexpected_keys:" + str(load_results.unexpected_keys))

        extra_agent_name = hypes['model']['args']['defor_encoder_fusion']['agent_names'][1]
        print("tuning parameters:")
        for name, value in model.named_parameters():
            # only tune Lora and the bias of the BN layers
            if 'adapters' in name and 'ego' not in name:  # non-ego adapters
                if 'conv0' in name: # lora parameters
                    value.requires_grad = True
                    print(name)
                else:
                    value.requires_grad = False
            else:
                if extra_agent_name in name:
                    value.requires_grad = True
                    print(name)
                else:
                    value.requires_grad = False
        optimizer = train_utils.setup_optimizer(hypes, model)
        
    elif hypes['train_agent_ID'] == -4:
        # back alignment
        ego_model_dict = torch.load(hypes['method_ego_path'])
        load_results = model.load_state_dict(ego_model_dict, strict=False)
        print("load unexpected_keys:" + str(load_results.unexpected_keys))

        extra_agent_name = hypes['model']['args']['defor_encoder_fusion']['agent_names'][1]
        print("tuning parameters:")
        for name, value in model.named_parameters():
            # tune paeameters assiaated with agent_names and model i
            if extra_agent_name in name or 'model_i' in name:
                value.requires_grad = True
                print(name)
            else:
                value.requires_grad = False
        # setup optimizer
        params = filter(lambda p: p.requires_grad, model.parameters())
        print("number of parameters tuned: {}".format(sum([c.numel() for c in params])))
        optimizer = train_utils.setup_optimizer(hypes, params)

    # record lowest validation loss checkpoint.
    lowest_val_loss = 1e5
    lowest_val_epoch = -1

    # define the loss
    criterion = train_utils.create_loss(hypes)

    # optimizer setup
    optimizer = train_utils.setup_optimizer(hypes, model)
    # lr scheduler setup
    

    # if we want to train from last checkpoint.
    if opt.model_dir:
        saved_path = opt.model_dir
        init_epoch, model = train_utils.load_saved_model(saved_path, model)
        lowest_val_epoch = init_epoch
        scheduler = train_utils.setup_lr_schedular(hypes, optimizer, init_epoch=init_epoch)
        print(f"resume from {init_epoch} epoch.")

    else:
        init_epoch = 0
        # if we train the model from scratch, we need to create a folder
        # to save the model,
        saved_path = train_utils.setup_train(hypes)
        scheduler = train_utils.setup_lr_schedular(hypes, optimizer)

    # we assume gpu is necessary
    if torch.cuda.is_available():
        model.to(device)
        
    # record training
    writer = SummaryWriter(saved_path)

    print('Training start')
    epoches = hypes['train_params']['epoches']
    supervise_single_flag = False if not hasattr(opencood_train_dataset, "supervise_single") else opencood_train_dataset.supervise_single
    # used to help schedule learning rate

    for epoch in range(init_epoch, max(epoches, init_epoch)):
        for param_group in optimizer.param_groups:
            print('learning rate %f' % param_group["lr"])
        
        cav_id = hypes['train_agent_ID']
        # the model will be evaluation mode during validation 
        model.train()
        if cav_id == -1 or cav_id == -3:
            model.eval()  # we call eval(), just to avoid the norm layer update
            extra_agent_name = hypes['model']['args']['defor_encoder_fusion']['agent_names'][1]
            # print("module to train:")
            for name, module in model.named_modules():
                if extra_agent_name in name:
                    # print(name)
                    module.train()

        for i, batch_data in enumerate(train_loader):
            if batch_data is None or batch_data['ego']['object_bbx_mask'].sum()==0:
                continue

            model.zero_grad()
            optimizer.zero_grad()
            batch_data = train_utils.to_device(batch_data, device)
            batch_data['ego']['epoch'] = epoch
            
            ouput_dict = model(batch_data)
            
            # train stage
            final_loss = 0
            if cav_id < 0: # 协同
                final_loss += criterion(ouput_dict, batch_data['ego']['label_dict']) # 协同的loss
                criterion.logging(epoch, i, len(train_loader), writer)
            else:
                # supervise_single_flag          
                final_loss += criterion(ouput_dict, batch_data[cav_id]['label_dict_single'])
                criterion.logging(epoch, i, len(train_loader), writer)

            # back-propagation
            final_loss.backward()
            optimizer.step()

            # torch.cuda.empty_cache()

        if epoch % hypes['train_params']['eval_freq'] == 0:
            valid_ave_loss = []

            with torch.no_grad():
                model.eval()
                for i, batch_data in enumerate(val_loader):
                    if batch_data is None:
                        continue
                    model.zero_grad()
                    optimizer.zero_grad()
                    batch_data = train_utils.to_device(batch_data, device)
                    batch_data['ego']['epoch'] = epoch
                    ouput_dict = model(batch_data)

                    final_loss = 0
                    if cav_id < 0: # 协同
                        final_loss += criterion(ouput_dict, batch_data['ego']['label_dict']) # 协同的loss
                        criterion.logging(epoch, i, len(train_loader), writer)
                    else:
                        # supervise_single_flag          
                        final_loss += criterion(ouput_dict, batch_data[cav_id]['label_dict_single'])
                        criterion.logging(epoch, i, len(train_loader), writer)
                    valid_ave_loss.append(final_loss.item())

            valid_ave_loss = statistics.mean(valid_ave_loss)
            print('At epoch %d, the validation loss is %f' % (epoch,
                                                              valid_ave_loss))
            writer.add_scalar('Validate_Loss', valid_ave_loss, epoch)

            # lowest val loss
            if valid_ave_loss < lowest_val_loss:
                lowest_val_loss = valid_ave_loss
                torch.save(model.state_dict(),
                       os.path.join(saved_path,
                                    'net_epoch_bestval_at%d.pth' % (epoch + 1)))
                if lowest_val_epoch != -1 and os.path.exists(os.path.join(saved_path,
                                    'net_epoch_bestval_at%d.pth' % (lowest_val_epoch))):
                    os.remove(os.path.join(saved_path,
                                    'net_epoch_bestval_at%d.pth' % (lowest_val_epoch)))
                lowest_val_epoch = epoch + 1

        if epoch % hypes['train_params']['save_freq'] == 0:
            torch.save(model.state_dict(),
                       os.path.join(saved_path,
                                    'net_epoch%d.pth' % (epoch + 1)))
        scheduler.step(epoch)

        opencood_train_dataset.reinitialize()

    print('Training Finished, checkpoints saved to %s' % saved_path)

    run_test = True    
    # ddp training may leave multiple bestval
    bestval_model_list = glob.glob(os.path.join(saved_path, "net_epoch_bestval_at*"))
    
    if len(bestval_model_list) > 1:
        import numpy as np
        bestval_model_epoch_list = [eval(x.split("/")[-1].lstrip("net_epoch_bestval_at").rstrip(".pth")) for x in bestval_model_list]
        ascending_idx = np.argsort(bestval_model_epoch_list)
        for idx in ascending_idx:
            if idx != (len(bestval_model_list) - 1):
                os.remove(bestval_model_list[idx])

    if run_test:
        fusion_method = opt.fusion_method
        if 'noise_setting' in hypes and hypes['noise_setting']['add_noise']:
            cmd = f"python opencood/tools/inference_w_noise.py --model_dir {saved_path} --fusion_method {fusion_method}"
        else:
            cmd = f"/cephyr/users/junjiewa/Alvis/software/miniconda3/envs/Where2comm/bin/python opencood/tools/inference.py --model_dir {saved_path} --fusion_method {fusion_method}"
        print(f"Running command: {cmd}")
        os.system(cmd)

if __name__ == '__main__':
    main()

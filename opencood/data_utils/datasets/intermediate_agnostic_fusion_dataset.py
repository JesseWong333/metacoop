# intermediate fusion dataset
import random
import math
from collections import OrderedDict
import numpy as np
import torch
import copy
from icecream import ic
from PIL import Image
import pickle as pkl
from opencood.utils import box_utils as box_utils
from opencood.data_utils.pre_processor import build_preprocessor
from opencood.data_utils.post_processor import build_postprocessor
from opencood.utils.camera_utils import (
    sample_augmentation,
    img_transform,
    normalize_img,
    img_to_tensor,
)
from opencood.utils.heter_utils import AgentSelector
from opencood.utils.common_utils import merge_features_to_dict
from opencood.utils.transformation_utils import x1_to_x2, x_to_world, get_pairwise_transformation
from opencood.utils.pose_utils import add_noise_data_dict
from opencood.utils.pcd_utils import (
    mask_points_by_range,
    mask_ego_points,
    shuffle_points,
    downsample_lidar_minimum,
)
from opencood.utils.common_utils import read_json
from opencood.data_utils.augmentor.data_augmentor import DataAugmentor


def getAgnosticFusionDataset(cls):
    """
    cls: the Basedataset.
    """
    class AgnosticFusionDataset(cls):
        def __init__(self, params, visualize, train=True):
            super().__init__(params, visualize, train)
            # intermediate and supervise single
            self.supervise_single = True if ('supervise_single' in params['model']['args'] and params['model']['args']['supervise_single']) \
                                        else False
            self.proj_first = False if 'proj_first' not in params['fusion']['args']\
                                         else params['fusion']['args']['proj_first']

            self.anchor_box_i = self.post_processor_i.generate_anchor_box()
            self.anchor_box_i_torch = torch.from_numpy(self.anchor_box_i)

            self.anchor_box_v = self.post_processor_v.generate_anchor_box()
            self.anchor_box_v_torch = torch.from_numpy(self.anchor_box_v)

            self.anchor_box = self.anchor_box_v
            self.anchor_box_torch = self.anchor_box_v_torch

            self.create_seg = params['seg_task'] if 'seg_task' in params else False

            # augmenter related
            if 'data_augment' in params:
                self.augment_config = params['data_augment']
                self.data_augmentor = DataAugmentor(params['data_augment'],
                                                    train,
                                                    intermediate=True)
                self.lidar_augment = True
            else:
                self.lidar_augment = False

            self.kd_flag = params.get('kd_flag', False)

            self.box_align = False
            if "box_align" in params:
                self.box_align = True
                self.stage1_result_path = params['box_align']['train_result'] if train else params['box_align']['val_result']
                self.stage1_result = read_json(self.stage1_result_path)
                self.box_align_args = params['box_align']['args']

        def generate_augment(self):
            flip = [None, None, None]
            noise_rotation = None
            noise_scale = None

            for aug_ele in self.augment_config:
                # for intermediate fusion only
                if 'random_world_rotation' in aug_ele['NAME']:
                    rot_range = \
                        aug_ele['WORLD_ROT_ANGLE']
                    if not isinstance(rot_range, list):
                        rot_range = [-rot_range, rot_range]
                    noise_rotation = np.random.uniform(rot_range[0],
                                                            rot_range[1])

                if 'random_world_flip' in aug_ele['NAME']:
                    for i, cur_axis in enumerate(aug_ele['ALONG_AXIS_LIST']):
                        enable = np.random.choice([False, True], replace=False,
                                                p=[0.5, 0.5])
                        flip[i] = enable

                if 'random_world_scaling' in aug_ele['NAME']:
                    scale_range = \
                        aug_ele['WORLD_SCALE_RANGE']
                    noise_scale = \
                        np.random.uniform(scale_range[0], scale_range[1])

            return flip, noise_rotation, noise_scale
           
        def get_item_single_car(self, selected_cav_base, ego_cav_base, cav_id):
            """
            Process a single CAV's information for the train/test pipeline.


            Parameters
            ----------
            selected_cav_base : dict
                The dictionary contains a single CAV's raw information.
                including 'params', 'camera_data'
            ego_pose : list, length 6
                The ego vehicle lidar pose under world coordinate.
            ego_pose_clean : list, length 6
                only used for gt box generation

            Returns
            -------
            selected_cav_processed : dict
                The dictionary contains the cav's processed information.
            """
            selected_cav_processed = {}
            ego_pose, ego_pose_clean = ego_cav_base['params']['lidar_pose'], ego_cav_base['params']['lidar_pose_clean']
     
            # calculate the transformation matrix
            transformation_matrix = \
                x1_to_x2(selected_cav_base['params']['lidar_pose'],
                        ego_pose) # T_ego_cav
            transformation_matrix_clean = \
                x1_to_x2(selected_cav_base['params']['lidar_pose_clean'],
                        ego_pose_clean)
            
            # lidar
            if self.load_lidar_file or self.visualize:
                # process lidar
                lidar_np = selected_cav_base['lidar_np']
                lidar_np = shuffle_points(lidar_np)
                # remove points that hit itself
                lidar_np = mask_ego_points(lidar_np)
                # project the lidar to ego space
                # x,y,z in ego space
                projected_lidar = \
                    box_utils.project_points_by_matrix_torch(lidar_np[:, :3],
                                                                transformation_matrix)
                if self.proj_first: 
                    lidar_np[:, :3] = projected_lidar

                if self.visualize:
                    # filter lidar
                    selected_cav_processed.update({'projected_lidar': projected_lidar})
                
                if self.kd_flag:
                    lidar_proj_np = copy.deepcopy(lidar_np)
                    lidar_proj_np[:,:3] = projected_lidar
                    selected_cav_processed.update({'projected_lidar': lidar_proj_np})
    
            # generate targets label single GT, note the reference pose is itself.
            object_bbx_center_single, object_bbx_mask_single, object_ids_single = self.generate_object_center_single(
                [selected_cav_base], selected_cav_base['params']['lidar_pose']
            )
            # data augmentation
            if self.lidar_augment:
                lidar_np, object_bbx_center_single, object_bbx_mask_single = \
                self.augment(lidar_np, object_bbx_center_single, object_bbx_mask_single,
                            selected_cav_base['flip'],
                            selected_cav_base['noise_rotation'],
                         selected_cav_base['noise_scale'])
            if self.load_lidar_file:
                if cav_id == 0: 
                    processed_lidar = self.pre_processor_v.preprocess(lidar_np)
                else:
                    processed_lidar = self.pre_processor_i.preprocess(lidar_np)
                selected_cav_processed.update({'processed_features': processed_lidar})
        
            if cav_id == 0:
                label_dict_single = self.post_processor_v.generate_label(
                    gt_box_center=object_bbx_center_single, anchors=self.anchor_box_v, mask=object_bbx_mask_single
                )
            else:
                label_dict_single = self.post_processor_i.generate_label(
                    gt_box_center=object_bbx_center_single, anchors=self.anchor_box_i, mask=object_bbx_mask_single
                )
            
            seg_label = self.create_seg_mask(object_bbx_center_single, object_bbx_mask_single) if self.create_seg else None

            selected_cav_processed.update({
                                "single_label_dict": label_dict_single,
                                "single_object_bbx_center": object_bbx_center_single,
                                "single_object_bbx_mask": object_bbx_mask_single,
                                "single_object_ids": object_ids_single,
                                "single_seg_label": seg_label})

            # camera
            if self.load_camera_file:
                camera_data_list = selected_cav_base["camera_data"]

                params = selected_cav_base["params"]
                imgs = []
                rots = []
                trans = []
                intrins = []
                extrinsics = []
                post_rots = []
                post_trans = []

                for idx, img in enumerate(camera_data_list):
                    camera_to_lidar, camera_intrinsic = self.get_ext_int(params, idx)

                    intrin = torch.from_numpy(camera_intrinsic)
                    rot = torch.from_numpy(
                        camera_to_lidar[:3, :3]
                    )  # R_wc, we consider world-coord is the lidar-coord
                    tran = torch.from_numpy(camera_to_lidar[:3, 3])  # T_wc

                    post_rot = torch.eye(2)
                    post_tran = torch.zeros(2)

                    img_src = [img]

                    # depth
                    if self.load_depth_file:
                        depth_img = selected_cav_base["depth_data"][idx]
                        img_src.append(depth_img)
                    else:
                        depth_img = None

                    # data augmentation
                    resize, resize_dims, crop, flip, rotate = sample_augmentation(
                        self.data_aug_conf, self.train
                    )
                    img_src, post_rot2, post_tran2 = img_transform(
                        img_src,
                        post_rot,
                        post_tran,
                        resize=resize,
                        resize_dims=resize_dims,
                        crop=crop,
                        flip=flip,
                        rotate=rotate,
                    )
                    # for convenience, make augmentation matrices 3x3
                    post_tran = torch.zeros(3)
                    post_rot = torch.eye(3)
                    post_tran[:2] = post_tran2
                    post_rot[:2, :2] = post_rot2

                    # decouple RGB and Depth

                    img_src[0] = normalize_img(img_src[0])
                    if self.load_depth_file:
                        img_src[1] = img_to_tensor(img_src[1]) * 255

                    imgs.append(torch.cat(img_src, dim=0))
                    intrins.append(intrin)
                    extrinsics.append(torch.from_numpy(camera_to_lidar))
                    rots.append(rot)
                    trans.append(tran)
                    post_rots.append(post_rot)
                    post_trans.append(post_tran)
                    

                selected_cav_processed.update(
                    {
                    "image_inputs": 
                        {
                            "imgs": torch.stack(imgs), # [Ncam, 3or4, H, W]
                            "intrins": torch.stack(intrins),
                            "extrinsics": torch.stack(extrinsics),
                            "rots": torch.stack(rots),
                            "trans": torch.stack(trans),
                            "post_rots": torch.stack(post_rots),
                            "post_trans": torch.stack(post_trans),
                        }
                    }
                )

            # anchor box
            if cav_id == 0:
                selected_cav_processed.update({"anchor_box": self.anchor_box_v})
            else:
                selected_cav_processed.update({"anchor_box": self.anchor_box_i})

            # note the reference pose ego
            object_bbx_center, object_bbx_mask, object_ids = self.generate_object_center([selected_cav_base],
                                                        ego_pose_clean)

            selected_cav_processed.update(
                {
                    "object_bbx_center": object_bbx_center[object_bbx_mask == 1],
                    "object_bbx_mask": object_bbx_mask,
                    "object_ids": object_ids,
                    'transformation_matrix': transformation_matrix,
                    'transformation_matrix_clean': transformation_matrix_clean
                }
            )

            return selected_cav_processed

        def __getitem__(self, idx):
            base_data_dict = self.retrieve_base_data(idx)
            base_data_dict = add_noise_data_dict(base_data_dict,self.params['noise_setting'])

            if self.lidar_augment:
                flip, noise_rotation, noise_scale = self.generate_augment()

            processed_data_dict = OrderedDict()
            for cav_id in base_data_dict:
                processed_data_dict[cav_id] = {} # 存放single-agent的信息
            processed_data_dict['ego'] = {} # ego存放通用的信息 

            ego_id = -1
            ego_lidar_pose = []
            ego_cav_base = None

            # first find the ego vehicle's lidar pose
            for cav_id, cav_content in base_data_dict.items():
                if cav_content['ego']:
                    ego_id = cav_id
                    ego_lidar_pose = cav_content['params']['lidar_pose']
                    ego_cav_base = cav_content
                    break
                
            assert cav_id == list(base_data_dict.keys())[
                0], "The first element in the OrderedDict must be ego"
            assert ego_id != -1
            assert len(ego_lidar_pose) > 0

            agents_image_inputs = []
            processed_features = []
            object_stack = []
            object_id_stack = []
            single_label_list = []
            single_object_bbx_center_list = []
            single_object_bbx_mask_list = []
            too_far = []
            lidar_pose_list = []
            lidar_pose_clean_list = []
            cav_id_list = []
            projected_lidar_clean_list = [] # disconet

            if self.visualize or self.kd_flag:
                projected_lidar_stack = []

            # loop over all CAVs to process information
            for cav_id, selected_cav_base in base_data_dict.items():
                # check if the cav is within the communication range with ego
                distance = \
                    math.sqrt((selected_cav_base['params']['lidar_pose'][0] -
                            ego_lidar_pose[0]) ** 2 + (
                                    selected_cav_base['params'][
                                        'lidar_pose'][1] - ego_lidar_pose[
                                        1]) ** 2)

                # if distance is too far, we will just skip this agent
                if distance > self.params['comm_range']:
                    too_far.append(cav_id)
                    continue

                lidar_pose_clean_list.append(selected_cav_base['params']['lidar_pose_clean'])
                lidar_pose_list.append(selected_cav_base['params']['lidar_pose']) # 6dof pose
                cav_id_list.append(cav_id)   

            for cav_id in too_far:
                base_data_dict.pop(cav_id)

            pairwise_t_matrix = \
                get_pairwise_transformation(base_data_dict,
                                                self.max_cav,
                                                self.proj_first)

            lidar_poses = np.array(lidar_pose_list).reshape(-1, 6)  # [N_cav, 6]
            lidar_poses_clean = np.array(lidar_pose_clean_list).reshape(-1, 6)  # [N_cav, 6]
            
            # merge preprocessed features from different cavs into the same dict
            cav_num = len(cav_id_list)
            
            for _i, cav_id in enumerate(cav_id_list):
                selected_cav_base = base_data_dict[cav_id]

                if self.lidar_augment:
                    selected_cav_base['flip'] = flip
                    selected_cav_base['noise_rotation'] = noise_rotation
                    selected_cav_base['noise_scale'] = noise_scale

                selected_cav_processed = self.get_item_single_car(
                    selected_cav_base,
                    ego_cav_base, cav_id)
                    
                object_stack.append(selected_cav_processed['object_bbx_center'])
                object_id_stack += selected_cav_processed['object_ids']
                if self.load_lidar_file:
                    # processed_features.append(
                    #     selected_cav_processed['processed_features'])
                    processed_data_dict[cav_id]['processed_lidar'] = selected_cav_processed['processed_features']
                if self.load_camera_file:
                    # agents_image_inputs.append(
                    #     selected_cav_processed['image_inputs'])
                    processed_data_dict[cav_id]['image_inputs'] = selected_cav_processed['image_inputs']

                if self.visualize:
                    projected_lidar_stack.append(
                        selected_cav_processed['projected_lidar'])
              
                if self.supervise_single:
                    # single_label_list.append(selected_cav_processed['single_label_dict'])
                    # single_object_bbx_center_list.append(selected_cav_processed['single_object_bbx_center'])
                    # single_object_bbx_mask_list.append(selected_cav_processed['single_object_bbx_mask'])
                    processed_data_dict[cav_id]['single_label_dict'] = self.post_processor.collate_batch([selected_cav_processed['single_label_dict']])
                    processed_data_dict[cav_id]['single_object_bbx_center'] = torch.from_numpy(selected_cav_processed['single_object_bbx_center']).unsqueeze(0)
                    processed_data_dict[cav_id]['single_object_bbx_mask'] = torch.from_numpy(selected_cav_processed['single_object_bbx_mask']).unsqueeze(0)
                    processed_data_dict[cav_id]['single_object_ids'] = selected_cav_processed['single_object_ids']
                    processed_data_dict[cav_id]['transformation_matrix'] = selected_cav_processed['transformation_matrix']
                    processed_data_dict[cav_id]['anchor_box'] = selected_cav_processed['anchor_box']
                    processed_data_dict[cav_id]['single_seg_label'] = torch.from_numpy(selected_cav_processed['single_seg_label']).unsqueeze(0) if selected_cav_processed['single_seg_label'] is not None else None

            # generate single view GT label
            # if self.supervise_single:
            #     single_label_dicts = self.post_processor.collate_batch(single_label_list)
            #     single_object_bbx_center = torch.from_numpy(np.array(single_object_bbx_center_list))
            #     single_object_bbx_mask = torch.from_numpy(np.array(single_object_bbx_mask_list))
            #     processed_data_dict['ego'].update({
            #         "single_label_dict_torch": single_label_dicts,
            #         "single_object_bbx_center_torch": single_object_bbx_center,
            #         "single_object_bbx_mask_torch": single_object_bbx_mask,
            #         })

            # exclude all repetitive objects    
            unique_indices = \
                [object_id_stack.index(x) for x in set(object_id_stack)]
            object_stack = np.vstack(object_stack)
            object_stack = object_stack[unique_indices]

            # make sure bounding boxes across all frames have the same number
            object_bbx_center = np.zeros((100, 7)) # todo: magic number, should be max_num
            mask = np.zeros(100)

            object_bbx_center[:object_stack.shape[0], :] = object_stack
            mask[:object_stack.shape[0]] = 1
            
            # generate targets label
            label_dict = \
                self.post_processor.generate_label(
                    gt_box_center=object_bbx_center,
                    anchors=self.anchor_box,
                    mask=mask)

            processed_data_dict['ego'].update(
                {'object_bbx_center': object_bbx_center,
                'object_bbx_mask': mask,
                'object_ids': [object_id_stack[i] for i in unique_indices],
                'anchor_box': self.anchor_box,
                'label_dict': label_dict,
                'cav_num': cav_num,
                'pairwise_t_matrix': pairwise_t_matrix,
                'lidar_poses_clean': lidar_poses_clean,
                'lidar_poses': lidar_poses})


            if self.visualize:
                processed_data_dict['ego'].update({'origin_lidar':
                    np.vstack(
                        projected_lidar_stack)})


            processed_data_dict['ego'].update({'sample_idx': idx,
                                                'cav_id_list': cav_id_list})

            return processed_data_dict


        def collate_batch_train(self, batch):
            # Intermediate fusion is different the other two
            output_dict = {'ego': {}}
            for cav_id in batch[0]:
                output_dict[cav_id] = {}

            object_bbx_center = []
            object_bbx_mask = []
            object_ids = []
            
            # used to record different scenario
            record_len = []
            label_dict_list = []
            lidar_pose_list = []
            origin_lidar = []
            lidar_pose_clean_list = []

            # pairwise transformation matrix
            pairwise_t_matrix_list = []

            # disconet
            teacher_processed_lidar_list = []
            
            processed_lidar_list = {cav_id:[] for cav_id in batch[0] if cav_id != 'ego'}
            image_inputs_list = {cav_id:[] for cav_id in batch[0] if cav_id != 'ego'}
            ### 2022.10.10 single gt ####
            if self.supervise_single:
                pos_equal_one_single = {cav_id:[] for cav_id in batch[0] if cav_id != 'ego'}
                neg_equal_one_single = {cav_id:[] for cav_id in batch[0] if cav_id != 'ego'}
                targets_single = {cav_id:[] for cav_id in batch[0] if cav_id != 'ego'}
                object_bbx_center_single = {cav_id:[] for cav_id in batch[0] if cav_id != 'ego'}
                object_bbx_mask_single = {cav_id:[] for cav_id in batch[0] if cav_id != 'ego'}
                object_ids_single = {cav_id:[] for cav_id in batch[0] if cav_id != 'ego'}
                object_seg_label_single = {cav_id:[] for cav_id in batch[0] if cav_id != 'ego'}

            for i in range(len(batch)):
                ego_dict = batch[i].pop('ego')
                output_dict['ego'].update({
                    "sample_idx": ego_dict['sample_idx'],
                    "cav_id_list": ego_dict['cav_id_list']
                })
                object_bbx_center.append(ego_dict['object_bbx_center'])
                object_bbx_mask.append(ego_dict['object_bbx_mask'])
                object_ids.append(ego_dict['object_ids'])
                lidar_pose_list.append(ego_dict['lidar_poses']) # ego_dict['lidar_pose'] is np.ndarray [N,6]
                lidar_pose_clean_list.append(ego_dict['lidar_poses_clean'])
                  
                record_len.append(ego_dict['cav_num'])
                label_dict_list.append(ego_dict['label_dict'])
                pairwise_t_matrix_list.append(ego_dict['pairwise_t_matrix'])

                if self.visualize:
                    origin_lidar.append(ego_dict['origin_lidar'])

                if self.kd_flag:
                    teacher_processed_lidar_list.append(ego_dict['teacher_processed_lidar'])

                for cav_id, instance_dict in batch[i].items():
                    if self.load_lidar_file:
                        processed_lidar_list[cav_id].append(instance_dict['processed_lidar'])
                    if self.load_camera_file:
                        image_inputs_list[cav_id].append(instance_dict['image_inputs']) # different cav_num, ego_dict['image_inputs'] is dict.
                    ### 2022.10.10 single gt ####
                    if self.supervise_single:
                        pos_equal_one_single[cav_id].append(instance_dict['single_label_dict']['pos_equal_one'])
                        neg_equal_one_single[cav_id].append(instance_dict['single_label_dict']['neg_equal_one'])
                        targets_single[cav_id].append(instance_dict['single_label_dict']['targets'])
                        object_bbx_center_single[cav_id].append(instance_dict['single_object_bbx_center'])
                        object_bbx_mask_single[cav_id].append(instance_dict['single_object_bbx_mask'])
                        object_ids_single[cav_id].append(instance_dict['single_object_ids'])
                        if instance_dict['single_seg_label'] is not None:
                            object_seg_label_single[cav_id].append(instance_dict['single_seg_label'])

            # convert to numpy, (B, max_num, 7)
            object_bbx_center = torch.from_numpy(np.array(object_bbx_center))
            object_bbx_mask = torch.from_numpy(np.array(object_bbx_mask))

            if self.load_lidar_file:
                for cav_id in batch[0]:
                    merged_feature_dict = merge_features_to_dict(processed_lidar_list[cav_id])
                    processed_lidar_torch_dict = \
                        self.pre_processor.collate_batch(merged_feature_dict)
                    output_dict[cav_id].update({'processed_lidar': processed_lidar_torch_dict})

            if self.load_camera_file:
                for cav_id in batch[0]:
                    merged_image_inputs_dict = merge_features_to_dict(image_inputs_list[cav_id], merge='stack')
                    output_dict[cav_id].update({'image_inputs': merged_image_inputs_dict})
           
            
            output_dict[0].update({'anchor_box': self.anchor_box_v_torch})
            output_dict[1].update({'anchor_box': self.anchor_box_i_torch})

            record_len = torch.from_numpy(np.array(record_len, dtype=int))
            lidar_pose = torch.from_numpy(np.concatenate(lidar_pose_list, axis=0))
            lidar_pose_clean = torch.from_numpy(np.concatenate(lidar_pose_clean_list, axis=0))
            label_torch_dict = \
                self.post_processor.collate_batch(label_dict_list)

            # for centerpoint
            label_torch_dict.update({'object_bbx_center': object_bbx_center,
                                     'object_bbx_mask': object_bbx_mask})

            # (B, max_cav)
            pairwise_t_matrix = torch.from_numpy(np.array(pairwise_t_matrix_list))

            # add pairwise_t_matrix to label dict
            label_torch_dict['pairwise_t_matrix'] = pairwise_t_matrix
            label_torch_dict['record_len'] = record_len
            

            # object id is only used during inference, where batch size is 1.
            # so here we only get the first element.
            output_dict['ego'].update({'object_bbx_center': object_bbx_center,
                                    'object_bbx_mask': object_bbx_mask,
                                    'record_len': record_len,
                                    'label_dict': label_torch_dict,
                                    'object_ids': object_ids[0],
                                    'pairwise_t_matrix': pairwise_t_matrix,
                                    'lidar_pose_clean': lidar_pose_clean,
                                    'lidar_pose': lidar_pose,
                                    'anchor_box': self.anchor_box_torch})


            if self.visualize:
                origin_lidar = \
                    np.array(downsample_lidar_minimum(pcd_np_list=origin_lidar))
                origin_lidar = torch.from_numpy(origin_lidar)
                output_dict['ego'].update({'origin_lidar': origin_lidar})

            if self.kd_flag:
                teacher_processed_lidar_torch_dict = \
                    self.pre_processor.collate_batch(teacher_processed_lidar_list)
                output_dict['ego'].update({'teacher_processed_lidar':teacher_processed_lidar_torch_dict})

            if self.supervise_single:
                for cav_id in batch[0]:
                    output_dict[cav_id].update({
                        "label_dict_single":{
                                "pos_equal_one": torch.cat(pos_equal_one_single[cav_id], dim=0),
                                "neg_equal_one": torch.cat(neg_equal_one_single[cav_id], dim=0),
                                "targets": torch.cat(targets_single[cav_id], dim=0),
                                # for centerpoint
                                "object_bbx_center_single": torch.cat(object_bbx_center_single[cav_id], dim=0),
                                "object_bbx_mask_single": torch.cat(object_bbx_mask_single[cav_id], dim=0),
                                "object_ids_single": object_ids_single[cav_id][0],
                                "object_seg_label": torch.cat(object_seg_label_single[cav_id], dim=0) if len(object_seg_label_single[cav_id])>0 else None
                            },
                        "object_bbx_center_single": torch.cat(object_bbx_center_single[cav_id], dim=0),
                        "object_bbx_mask_single": torch.cat(object_bbx_mask_single[cav_id], dim=0),
                        "object_ids_single": object_ids_single[cav_id][0]
                    })

            return output_dict

        def collate_batch_test(self, batch):
            assert len(batch) <= 1, "Batch size 1 is required during testing!"
            output_dict = self.collate_batch_train(batch)
            if output_dict is None:
                return None

            transformation_matrix_torch = \
                torch.from_numpy(np.identity(4)).float()
            transformation_matrix_clean_torch = \
                torch.from_numpy(np.identity(4)).float()

            output_dict['ego'].update({'transformation_matrix':
                                        transformation_matrix_torch,
                                        'transformation_matrix_clean':
                                        transformation_matrix_clean_torch,})

            # output_dict['ego'].update({
            #     "sample_idx": batch[0]['ego']['sample_idx'],
            #     "cav_id_list": batch[0]['ego']['cav_id_list']
            # })

            return output_dict


        def post_process(self, data_dict, output_dict):
            """
            Process the outputs of the model to 2D/3D bounding box.

            Parameters
            ----------
            data_dict : dict
                The dictionary containing the origin input data of model.

            output_dict :dict
                The dictionary containing the output of the model.

            Returns
            -------
            pred_box_tensor : torch.Tensor
                The tensor of prediction bounding box after NMS.
            gt_box_tensor : torch.Tensor
                The tensor of gt bounding box.
            """
            pred_box_tensor, pred_score = \
                self.post_processor.post_process(data_dict, output_dict)
            gt_box_tensor = self.post_processor.generate_gt_bbx(data_dict)

            return pred_box_tensor, pred_score, gt_box_tensor


    return AgnosticFusionDataset



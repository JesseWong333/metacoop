# author Junjie Wang
# from BEVformer 

# 需要支持任意大小，任意数量的feature fusion
import torch
import torch.nn as nn
import math
from collections import OrderedDict
from opencood.utils.mmcv_utils import constant_init, xavier_init
from opencood.models.sub_modules.torch_transformation_utils import warp_affine_simple
# from mmcv.ops.multi_scale_deform_attn import multi_scale_deformable_attn_pytorch # use the pytorch version;
from opencood.utils.ms_deform_attn_ops.functions import MSDeformAttnFunction
from mmdet.models.utils import LearnedPositionalEncoding
from torch.nn.init import normal_

import loralib as lora 

# SelfAttn 和 CrossAttn 可以通用
class DeforAttn(nn.Module):
    def __init__(self, embed_dims, num_heads=1, num_points=4, dropout=0.1, agent_names=['ego'], feature_levels=[3], lora_rank=0):
        super().__init__()

        self.im2col_step = 64
        self.agent_names = agent_names
        self.feature_levels = feature_levels
        self.num_heads = num_heads
        self.num_points = num_points
        
        sampling_offsets_dict = OrderedDict()
        attention_weights_dict = OrderedDict()
        for agent_name, feature_level in zip(agent_names, feature_levels):
            sampling_offsets_dict[agent_name] = nn.Linear(embed_dims, feature_level*num_heads*num_points * 2)  # 对于self-attention 这个要不要调节？
            attention_weights_dict[agent_name] = nn.Linear(embed_dims, feature_level*num_heads*num_points)
        
        self.sampling_offsets = nn.ModuleDict(sampling_offsets_dict)
        self.attention_weights = nn.ModuleDict(attention_weights_dict)

        self.dropout = nn.Dropout(dropout)

        self.value_proj = lora.Linear(embed_dims, embed_dims, r = lora_rank) # 这个是参数的大头
        self.output_proj = lora.Linear(embed_dims, embed_dims, r = lora_rank)
        self.init_weights()  # bug: 之前没调用
       
    def init_weights(self):
        """Default initialization for Parameters of Module."""
        thetas = torch.arange(
            self.num_heads,
            dtype=torch.float32) * (2.0 * math.pi / self.num_heads)
        grid_init = torch.stack([thetas.cos(), thetas.sin()], -1)
        grid_init = (grid_init /
                     grid_init.abs().max(-1, keepdim=True)[0]).view(
            self.num_heads, 1, 1,
            2).repeat(1, 1, self.num_points, 1)

        for i in range(self.num_points):
            grid_init[:, :, i, :] *= i + 1

        for i, (key, module) in enumerate(self.sampling_offsets.items()): 
            constant_init(module, 0.)
            grid_init_lvl = grid_init.repeat(1, self.feature_levels[i], 1, 1)
            module.bias.data = grid_init_lvl.view(-1)

        for key, module in self.attention_weights.items():
            constant_init(module, val=0., bias=0.)
        xavier_init(self.value_proj, distribution='uniform', bias=0.)
        xavier_init(self.output_proj, distribution='uniform', bias=0.)
    
    def sampling_offsets_forward(self, x):
        _, num_query, _ = x.shape
        outs = []   
        for i, (key, module) in enumerate(self.sampling_offsets.items()):
            out = module(x) # 1, feature_level*num_heads*num_points * 2
            out = out.view(1, num_query, self.num_heads, self.feature_levels[i], self.num_points, 2)
            outs.append(out)
        return torch.cat(outs, dim=3)
    
    def attention_weights_forward(self, x):
        _, num_query, _ = x.shape
        outs = []
        for i, (key, module) in enumerate(self.attention_weights.items()):
            out = module(x) # 1, feature_level*num_heads*num_points
            out = out.view(1, num_query, self.num_heads, self.feature_levels[i], self.num_points)
            outs.append(out)
        return torch.cat(outs, dim=3)

    def forward(self, query, query_pos, value, reference_points, spatial_shapes, *args):
        """
        Args:
            query: [1, H*W, C]
            query_pos: [1, H*W, C]
            value: [1, h*w+...+, C],   [1, H*W, C] for self attention   N = 要适应不同的不同大小的特征; 所有的feature加在第二维度
            reference_points: [1, H*W, sum(feature_levels), 2], [1, H*W, 1, 2] for self attention
            spatial_shapes: [sum(feature_levels), 2] # [1, 2] for self attention

        Returns:
            _type_: _description_
        """
        identity = query

        query = query + query_pos

        _, num_value, _ = value.shape
        
        value = self.value_proj(value)  # 这个要不要分开， 不同agent不一样？
        value = value.unsqueeze(0).contiguous().view(1, num_value, self.num_heads,-1) # [1, h*w+...+, n_head, C//n_head]

        _, num_query, C = query.shape  # num_query = H*W

        sampling_offsets = self.sampling_offsets_forward(query)
        attention_weights = self.attention_weights_forward(query) # 1, num_query, self.num_heads, self.feature_level, self.num_points

        attention_weights = attention_weights.view(1, num_query, self.num_heads, sum(self.feature_levels)*self.num_points)
        attention_weights = attention_weights.softmax(-1)
        attention_weights = attention_weights.view(1, num_query,
                                                self.num_heads,
                                                sum(self.feature_levels),
                                                self.num_points).contiguous()
        level_start_index = torch.cat((spatial_shapes.new_zeros((1, )), spatial_shapes.prod(1).cumsum(0)[:-1])) # used for cuda MSdeformal ops
        offset_normalizer = torch.stack(
                [spatial_shapes[..., 1], spatial_shapes[..., 0]], -1) # [N*self.feature_level, 2] 
        # [1, H*W, N*feature_level, 2]-> [1, H*W, 1, N*feature_level, 1, 2] + [1, H*W, n_head, N*self.feature_level, n_point, 2]\[1, 1, 1, N*self.feature_level, 1, 2]
        sampling_locations = reference_points[:, :, None, :, None, :] \
                + sampling_offsets \
                / offset_normalizer[None, None, None, :, None, :]  # sampling_locations: range [0, 1], normalized, left-up corner[0, 0]
        
        output = MSDeformAttnFunction.apply(
                value, spatial_shapes, level_start_index, sampling_locations, attention_weights, self.im2col_step)
        # output = multi_scale_deformable_attn_pytorch(value, spatial_shapes, sampling_locations, attention_weights) # [1, h*w, c]
        
        output = self.output_proj(output)

        return self.dropout(output) + identity

class FFN(nn.Module):
    def __init__(self,
                 embed_dims=256,
                 feedforward_channels=1024,
                 num_fcs=2,
                 ffn_drop=0.1,
                 add_identity=True,
                 dropout_layer=None,
                 lora_rank = 0,
                 init_cfg=None,
                 **kwargs):
        super().__init__()
        assert num_fcs >= 2, 'num_fcs should be no less ' \
            f'than 2. got {num_fcs}.'
        self.embed_dims = embed_dims
        self.feedforward_channels = feedforward_channels
        self.num_fcs = num_fcs
        self.activate = nn.ReLU(inplace=True)

        layers = []
        in_channels = embed_dims
        for _ in range(num_fcs - 1):
            layers.append(
                nn.Sequential(
                    lora.Linear(in_channels, feedforward_channels, r=lora_rank), self.activate,
                    nn.Dropout(ffn_drop)))
            in_channels = feedforward_channels
        layers.append(lora.Linear(feedforward_channels, embed_dims, r=lora_rank))
        layers.append(nn.Dropout(ffn_drop))
        self.layers = nn.Sequential(*layers)
        self.dropout_layer = dropout_layer if dropout_layer else torch.nn.Identity()
        self.add_identity = add_identity

    def forward(self, x, identity = None):
        """Forward function for `FFN`.

        The function would add x to the output tensor if residue is None.
        """
        out = self.layers(x)
        if not self.add_identity:
            return self.dropout_layer(out)
        if identity is None:
            identity = x
        return identity + self.dropout_layer(out)

class Block(nn.Module):
    def __init__(self, embed_dims, num_heads_self=1, num_points_self=4, num_heads_cross=1, num_points_cross=4, dropout=0.1, agent_names=['ego'], feature_levels=[3], lora_rank_attn=0, lora_rank_ffn=0, cfgs = ["self_attn", "norm", "cross_attn", "norm", "ffn", "norm"]) -> None:
        super().__init__()
  
        block_layers = nn.ModuleList()
        for cfg in cfgs:
            if cfg == "self_attn":
                self_attn = DeforAttn(embed_dims, num_heads_self, num_points_self, dropout, ['ego'], [1], lora_rank_attn)
                block_layers.append(self_attn)
            elif cfg == "ffn":
                ffn = FFN(embed_dims, feedforward_channels=embed_dims*4, lora_rank=lora_rank_ffn)
                block_layers.append(ffn)
            elif cfg == "cross_attn":
                cross_attn = DeforAttn(embed_dims, num_heads_cross, num_points_cross, dropout, agent_names, feature_levels, lora_rank_attn)
                block_layers.append(cross_attn)
            elif cfg == "norm":
                block_layers.append(nn.LayerNorm(embed_dims))
        self.cfgs = cfgs
        self.block_layers = block_layers
    
    def forward(self, query, query_pos, value, ref_2d, spatial_shapes_cross, spatial_shapes_self):
        """_summary_

        Args:
            query: [1, H*W, C]
            query_pos: [1, H*W, C]
            value: [1, h*w+..., C],   [1, H*W, C] for self attention
            ref_2d: [1, H*W, sum(feature_levels), 2] 
            spatial_shapes_cross: [sum(feature_levels), 2]  # the shape [(h0, w0), (h1, w1), (h2, w2)]
            spatial_shapes_self: [1, 2]  # the shape (H, W)
        Returns:
            _type_: _description_
        """
        for layer_type,  layer in zip(self.cfgs, self.block_layers):
            if layer_type == "self_attn":
                query = layer(query, query_pos, query, ref_2d[:, :, :1, :], spatial_shapes_self)
            elif layer_type == "ffn" or layer_type == "norm":
                query = layer(query)
            elif layer_type == "cross_attn":
                query = layer(query, query_pos, value, ref_2d, spatial_shapes_cross)
        return query


class DeforEncoderMultiScale(nn.Module):
    def __init__(self, model_cfg) -> None:
        super().__init__()

        self.blocks = nn.ModuleList()

        block_cfgs = model_cfg["block_cfgs"]
        for block_cfg in block_cfgs:
            self.blocks.append(Block(*block_cfg))
        if 'train_stage' in model_cfg:
            self.train_stage = model_cfg['train_stage']

        self.bev_h = model_cfg["bev_h"] # 100
        self.bev_w = model_cfg["bev_w"] # 252
        self.embed_dims = model_cfg["embed_dims"]  # 384 按照原来的大小设置
        self.max_num_agent = model_cfg["max_num_agent"] # 
        self.feature_level = model_cfg["feature_level"] # should be 3

        self.bev_embedding = nn.Embedding(
                self.bev_h * self.bev_w, self.embed_dims)
        self.positional_encoding = LearnedPositionalEncoding(        
            num_feats=self.embed_dims//2,
            row_num_embed=self.bev_h,
            col_num_embed=self.bev_w)
        
        self.level_embeds = nn.Parameter(
            torch.Tensor(self.feature_level, self.embed_dims))
        self.agent_embeds = nn.Parameter(
            torch.Tensor(self.max_num_agent, self.embed_dims))
        normal_(self.level_embeds)
        normal_(self.agent_embeds)
        
        if "calibrate" in model_cfg:
            self.calibrate = model_cfg["calibrate"]
        else:
            self.calibrate = False

    
    @staticmethod
    def get_reference_points(H, W, bs=1, device='cuda', dtype=torch.float):
        # H, W is
        ref_y, ref_x = torch.meshgrid(
                torch.linspace(
                    0.5, H - 0.5, H, dtype=dtype, device=device),
                torch.linspace(
                    0.5, W - 0.5, W, dtype=dtype, device=device)
            )
        ref_y = ref_y.reshape(-1)[None] / H
        ref_x = ref_x.reshape(-1)[None] / W
        ref_2d = torch.stack((ref_x, ref_y), -1)
        ref_2d = ref_2d.repeat(bs, 1, 1).unsqueeze(2)
        return ref_2d
    
    def regroup(self, x, record_len):
        cum_sum_len = torch.cumsum(record_len, dim=0)
        split_x = torch.tensor_split(x, cum_sum_len[:-1].cpu())
        return split_x
    
    def forward(self, mlvl_feats, record_len, pairwise_t_matrix):
        """ multi-scale deformable attention
            mlvl_feats: [(Bs, C, h, w)] a list of multi-scale features
            offsets: [(Bs, h, w, 2)] a list with N-1 agents
            pred_offset:
        """
        mlvl_feats = [self.regroup(x, record_len) for x in mlvl_feats] # [[(2, C, H, W),...,(2, C, H, W)], [2, C, H1, W1)], []] # 3 by bs//2 list
        # batch level first
        split_x = [ [] for _ in range(len(mlvl_feats[0]))]
        for i, f_level in enumerate(mlvl_feats):
            for j, b_level in enumerate(f_level):
                split_x[j].append(b_level)

        out = []
        for b, xx in enumerate(split_x):  
            # input: xx: [ (N, C_0, H_0, W_0), (N, C_1, H_1, W_1), (N, C_2, H_2, W_2) ]
            N = xx[0].shape[0]
            t_matrix = pairwise_t_matrix[b][:N, :N, :, :]
          
            feat_flatten = []
            spatial_shapes = []
            # treat both the num of agent and feature level as feature level; intotal 6 feature levels
            for lvl, feat in enumerate(xx):
                _, c, h, w = feat.shape
                feat = warp_affine_simple(feat, t_matrix[0, :, :, :], (h, w))  # 0 is ego
                spatial_shape = (h, w)
                feat = feat.flatten(2).transpose(1, 2) # N, h*w, C
                feat = feat + self.level_embeds[None, lvl:lvl + 1, :].to(feat.dtype)
                feat = feat + self.agent_embeds[:N, None, :].to(feat.dtype)
                spatial_shapes.append(spatial_shape) 
                feat_flatten.append(feat)  # [N, h*w, C]
            
            feat_flatten = torch.cat(feat_flatten, 1) # N, H*W+...+H3*W3, C
            ref_2d = self.get_reference_points(
               self.bev_h, self.bev_w, device=feat.device, dtype=feat.dtype) # 1, H*W, 1, 2
           
            ref_2d = ref_2d.repeat(1, 1, N*self.feature_level, 1)  # #1, H*W+...+H3*W3, N, 2

            # spatial shapes [(h0,w0), (h1,w1), (h2,w2), (h0,w0), (h1,w1), (h2,w2)]
            spatial_shapes = spatial_shapes * N
            spatial_shapes = torch.as_tensor(spatial_shapes, dtype=torch.long, device=feat.device)

            spatial_shapes_self = [(self.bev_h, self.bev_w)]
            spatial_shapes_self = torch.as_tensor(spatial_shapes_self, dtype=torch.long, device=feat.device)

            bev_queries = self.bev_embedding.weight.to(feat.dtype)  # H*W, C
            bev_queries = bev_queries.unsqueeze(0) #  [1, H*W, C]
            bev_mask = torch.zeros((1, self.bev_h, self.bev_w),
                                device=bev_queries.device).to(feat.dtype)
            bev_pos = self.positional_encoding(bev_mask).to(feat.dtype) # [1, num_feats*2, h, w]
            bev_pos = bev_pos.flatten(2).permute(0, 2, 1) # [1, C, h*w]->[1, h*w, C] 

            # 
            for _, block in enumerate(self.blocks):
                bev_queries = block(bev_queries, bev_pos, feat_flatten, ref_2d, spatial_shapes, spatial_shapes_self)  # [1, H*W, C]
            
            bev_queries = bev_queries.permute(0, 2, 1).view(1, self.embed_dims, self.bev_h, self.bev_w)  # 就是这个问题，其他的不行也是因为我没有permute
            out.append(bev_queries)
       
        return torch.cat(out, dim=0)

    
import torch
import torch.nn as nn
import collections
import numpy as np
import random
import torchvision
from source import configs
from source.utils import stepfun
from source.utils import misc

# _cost_matric_cache = {}
# def _cost_matrix(batch_size, h, w, device : torch.device):
#     key = (batch_size, h, w, device)
#     if key in _cost_matric_cache:
#         c = _cost_matric_cache[key]
#     else:
#         a = torch.linspace(0.0, h - 1.0, h, device=device)
#         b = torch.linspace(0.0, w - 1.0, w, device=device)
#         y_grid = a.view(-1, 1).repeat(batch_size, 1, w) / h
#         x_grid = b.view(1, -1).repeat(batch_size, h, 1) / w
#         grids = torch.cat([y_grid.view(batch_size, -1, 1), x_grid.view(batch_size, -1, 1)], dim=-1)

#         x_col = grids.unsqueeze(2)
#         y_lin = grids.unsqueeze(1)
#         p = 2
#         # Returns the matrix of $|x_i-y_j|^p$.
#         c = torch.sum((torch.abs(x_col - y_lin))**p, -1)
#         _cost_matric_cache[key] = c
#     return c


def point_point_distance(points1, points2):
    """
    Calculate the minimal distance between points1 and points2.
    Args:
        points1: (..., 3), the points1
        points2: (..., 3), the points2
    Returns:
        dist: (...), the minimal distance between points1 and points2
    """
    dist = torch.sqrt(torch.sum(torch.square(points1 - points2), dim=-1))
    return dist


def point_segment_distance(points, segment_A, segment_B):
    """
    Calculate the minimal distance between points and line segments.
    Args:
        points: (..., 3), the points
        segment_A: (..., 3), the start point of the line segment
        segment_B: (..., 3), the end point of the line segment
    Returns:
        dist: (...), the minimal distance between points and line segments
    """
    AP = points - segment_A
    BP = points - segment_B
    AB = segment_B - segment_A
    AP_dot_AB = torch.sum(AP * AB, dim=-1)
    AB_square = torch.sum(torch.square(AB), dim=-1)
    AP_square = torch.sum(torch.square(AP), dim=-1)
    BP_square = torch.sum(torch.square(BP), dim=-1)
    t = AP_dot_AB / AB_square
    dist = torch.sqrt(torch.clamp(AP_square - t * AP_dot_AB, min=1e-8))
    distA = torch.sqrt(AP_square)
    distB = torch.sqrt(BP_square)
    dist = torch.where(torch.logical_and(0 < t, t < 1), dist, torch.minimum(distA, distB))
    return dist


def segment_segment_distance(segment1_A, segment1_B, segment2_A, segment2_B):
    """
    Calculate the minimal distance between two line segments.
    Args:
        segment1_A: (..., 3), the start point of the first line segment
        segment1_B: (..., 3), the end point of the first line segment
        segment2_A: (..., 3), the start point of the second line segment
        segment2_B: (..., 3), the end point of the second line segment
    Returns:
        dist: (...), the minimal distance between two line segments
    """
    r = segment2_A - segment1_A
    u = segment1_B - segment1_A
    v = segment2_B - segment2_A

    ru = torch.sum(r * u, dim=-1)
    rv = torch.sum(r * v, dim=-1)
    uu = torch.sum(u * u, dim=-1)
    uv = torch.sum(u * v, dim=-1)
    vv = torch.sum(v * v, dim=-1)

    det = uu * vv - uv * uv
    cond = det < 1e-6 * uu * vv
    s = torch.clamp(torch.where(cond, ru / uu, (ru * vv - rv * uv) / det), 0, 1)
    t = torch.where(cond, torch.zeros_like(det), torch.clamp((ru * uv - rv * uu) / det, 0, 1))

    S = torch.clamp((t * uv + ru) / uu, 0, 1)
    T = torch.clamp((s * uv - rv) / vv, 0, 1)

    A = segment1_A + u * S.unsqueeze(-1)
    B = segment2_A + v * T.unsqueeze(-1)
    dist = torch.sqrt(torch.sum(torch.square(A - B), dim=-1))
    return dist


def segment_segment_integrated_distance(segment1_A, segment1_B, segment2_A, segment2_B):
    """
    Calculate the integrated mean distance between two line segments.
    Args:
        segment1_A: (..., 3), the start point of the first line segment
        segment1_B: (..., 3), the end point of the first line segment
        segment2_A: (..., 3), the start point of the second line segment
        segment2_B: (..., 3), the end point of the second line segment
    Returns:
        dist: (...), the integrated mean distance between two line segments
    """
    A1A2 = segment1_A - segment2_A
    B1B2 = segment1_B - segment2_B
    dist = (torch.sum(A1A2 * A1A2, dim=-1) + torch.sum(B1B2 * B1B2, dim=-1) +
            torch.sum(A1A2 * B1B2, dim=-1)) / 3
    return dist


@torch.no_grad()
def _cost_matrix(points, weights, segment_A, segment_B):
    # weighted points to line segments distance matrix
    # dist = point_segment_distance(
    #     points[None, :, :, :],
    #     segment_A[:, None, None, :],
    #     segment_B[:, None, None, :],
    # )
    # weights_norm = weights / (torch.sum(weights, dim=-1, keepdim=True) + 1e-7)
    # cost_points = torch.sum(dist * weights_norm[None, :, :], dim=-1)
    # line segments to line segments distance matrix
    # cost_segments = segment_segment_distance(
    #     segment_A[:, None, :],
    #     segment_B[:, None, :],
    #     segment_A[None, :, :],
    #     segment_B[None, :, :],
    # )
    # cost = cost_points + cost_segments * (1 - torch.sum(weights, dim=-1))
    # integrated line segments to line segments distance matrix
    cost_segments = segment_segment_integrated_distance(
        segment_A[:, None, :],
        segment_B[:, None, :],
        segment_A[None, :, :],
        segment_B[None, :, :],
    )
    cost = cost_segments
    return cost


def _compute_sinkhorn_loss(C, epsilon, niter, mass_x, mass_y):
    """
    Given two emprical measures with n points each with locations x and y
    outputs an approximation of the OT cost with regularization parameter epsilon
    niter is the max. number of steps in sinkhorn loop
    """
    # normalize mass
    mass_x = torch.clamp(mass_x, min=0, max=1e9)
    mass_x = mass_x + 1e-9
    mu = (mass_x / mass_x.sum(dim=-1, keepdim=True)).to(C.device)

    mass_y = torch.clamp(mass_y, min=0, max=1e9)
    mass_y = mass_y + 1e-9
    nu = (mass_y / mass_y.sum(dim=-1, keepdim=True)).to(C.device)

    def M(u, v):
        """Modified cost for logarithmic updates
        $M_{ij} = (-c_{ij} + u_i + v_j) / \epsilon$"""
        return (-C + u.unsqueeze(2) + v.unsqueeze(1)) / epsilon

    def lse(A):
        "log-sum-exp"
        return torch.logsumexp(A, dim=2, keepdim=True)

    # Actual Sinkhorn loop ......................................................................
    u, v, err = 0. * mu, 0. * nu, 0.

    for i in range(niter):
        u = epsilon * (torch.log(mu) - lse(M(u, v)).squeeze()) + u
        v = epsilon * (torch.log(nu) - lse(M(u, v).transpose(1, 2)).squeeze()) + v

    pi = torch.exp(M(u, v))  # Transport plan pi = diag(a)*K*diag(b)
    cost = torch.sum(pi * C, dim=[1, 2])  # Sinkhorn cost

    return cost


def _get_sinkhorn_loss(rendering, ray_history, batch, config):
    # output = rendering['rgb'].permute(0, 3, 1, 2)
    # target = batch['rgb'].permute(0, 3, 1, 2)
    # batch_size, _, H, W = output.shape

    # # randomly select a color channel, to speedup and consume memory
    # i = random.randint(0, 2)
    # output = output[:, [i], :, :]
    # target = target[:, [i], :, :]

    # if max(H, W) > config.sinkhorn_patch_size:
    #     if H > W:
    #         W = int(config.sinkhorn_patch_size * W / H)
    #         H = config.sinkhorn_patch_size
    #     else:
    #         H = int(config.sinkhorn_patch_size * H / W)
    #         W = config.sinkhorn_patch_size
    #     output = nn.functional.interpolate(output, [H, W], mode='area')
    #     target = nn.functional.interpolate(target, [H, W], mode='area')

    # cost_matrix = _cost_matrix(batch_size, H, W, output.device)
    # sinkhorn_loss = _compute_sinkhorn_loss(cost_matrix,
    #                                        epsilon=0.1,
    #                                        niter=5,
    #                                        mass_x=output.reshape(batch_size, -1),
    #                                        mass_y=target.reshape(batch_size, -1))

    # randomly select a color channel, to speedup and consume memory
    num_channels = batch['rgb'].shape[-1]
    i = random.randint(0, num_channels - 1)
    output = rendering['rgb'][..., i].reshape(-1)
    target = batch['rgb'][..., i].reshape(-1)
    segment_A = (batch['origins'] + batch['directions'] * batch['near']).flatten(0, 2)
    segment_B = (batch['origins'] + batch['directions'] * batch['far']).flatten(0, 2)
    points = ray_history['coord'].flatten(0, 2)
    weights = ray_history['weights'].flatten(0, 2)

    sample_num = 1024
    # weights_sum = weights.sum(dim=-1)
    # sample_index = torch.topk(weights_sum, sample_num)[1]
    output = output[:sample_num]  # [sample_index]
    target = target[:sample_num]  # [sample_index]
    segment_A = segment_A[:sample_num]  # [sample_index]
    segment_B = segment_B[:sample_num]  # [sample_index]
    points = points[:sample_num]  # [sample_index]
    weights = weights[:sample_num]  # [sample_index]

    cost_matrix = _cost_matrix(points, weights, segment_A, segment_B)
    sinkhorn_loss = _compute_sinkhorn_loss(cost_matrix,
                                           epsilon=0.1,
                                           niter=5,
                                           mass_x=output.unsqueeze(0),
                                           mass_y=target.unsqueeze(0))

    return sinkhorn_loss


def _get_data_loss(residual_sq, residual_abs, config):
    if config.data_loss_type == 'mse':
        # Mean-squared error (L2) loss.
        data_loss = residual_sq
    elif config.data_loss_type == 'l1':
        # Mean-absolute error (L1) loss.
        data_loss = residual_abs
    elif config.data_loss_type == 'charb':
        # Charbonnier loss.
        data_loss = torch.sqrt(residual_sq + config.charb_padding**2)
    elif config.data_loss_type == 'huber':
        data_loss = torch.where(residual_abs < config.huber_delta, 0.5 * residual_sq,
                                config.huber_delta * (residual_abs - 0.5 * config.huber_delta))
    else:
        assert False, f'Unknown data loss type {config.data_loss_type}'
    return data_loss


def compute_data_loss(batch, renderings, ray_history, config: configs.Config):
    """Computes data loss terms for RGB, normal, and depth outputs."""
    data_losses = []
    mask_losses = []
    sinkhorn_losses = []
    stats = collections.defaultdict(lambda: [])
    use_mask_loss = config.mask_loss_mult > 0 and 'alphas' in batch
    use_sinkhorn_loss = config.sinkhorn_loss_mult > 0

    for level, rendering in enumerate(renderings):
        is_final_level = level == len(renderings) - 1

        if config.data_coarse_loss_mult > 0 or is_final_level:
            residual = rendering['rgb'] - batch['rgb'][..., :3]
            residual_sq = torch.square(residual)
            residual_abs = torch.abs(residual)
            stats['mses'].append(residual_sq.mean().item())
            stats['maes'].append(residual_abs.mean().item())

            data_loss = _get_data_loss(residual_sq, residual_abs, config)
            data_losses.append(data_loss.mean())

        if use_mask_loss and is_final_level:
            mask_residual = rendering['acc'] - batch['alphas']
            mask_residual_sq = torch.square(mask_residual)
            mask_residual_abs = torch.abs(mask_residual)
            stats['mask_mses'].append(mask_residual_sq.mean().item())
            stats['mask_maes'].append(mask_residual_abs.mean().item())

            mask_loss = _get_data_loss(mask_residual_sq, mask_residual_abs, config)
            mask_losses.append(mask_loss.mean())

        if use_sinkhorn_loss and is_final_level:
            sinkhorn_loss = _get_sinkhorn_loss(rendering, ray_history[level], batch, config)
            stats['sinkhorn_loss'].append(sinkhorn_loss.mean().item())
            sinkhorn_losses.append(sinkhorn_loss.mean())

    loss = (config.data_coarse_loss_mult * sum(data_losses[:-1]) +
            config.data_loss_mult * data_losses[-1])

    if use_mask_loss:
        loss += config.mask_loss_mult * mask_losses[-1]

    if use_sinkhorn_loss:
        loss += config.sinkhorn_loss_mult * sinkhorn_losses[-1]

    stats = {k: np.array(stats[k]) for k in stats}
    return loss, stats


def interlevel_loss(ray_history, config: configs.Config):
    """Computes the interlevel loss defined in mip-NeRF 360."""
    # Stop the gradient from the interlevel loss onto the NeRF MLP.
    last_ray_results = ray_history[-1]
    c = last_ray_results['sdist'].detach()
    w = last_ray_results['weights'].detach()
    loss_interlevel = torch.tensor(0., device=c.device)
    for ray_results in ray_history[:-1]:
        cp = ray_results['sdist']
        wp = ray_results['weights']
        loss_interlevel += stepfun.lossfun_outer(c, w, cp, wp).mean()
    return config.interlevel_loss_mult * loss_interlevel


def anti_interlevel_loss(ray_history, config: configs.Config):
    """Computes the interlevel loss defined in mip-NeRF 360."""
    last_ray_results = ray_history[-1]
    c = last_ray_results['sdist'].detach()
    w = last_ray_results['weights'].detach()
    w_normalize = w / (c[..., 1:] - c[..., :-1])
    loss_anti_interlevel = torch.tensor(0., device=c.device)
    for i, ray_results in enumerate(ray_history[:-1]):
        cp = ray_results['sdist']
        wp = ray_results['weights']
        c_, w_ = stepfun.blur_stepfun(c, w_normalize, config.pulse_width[i])

        # piecewise linear pdf to piecewise quadratic cdf
        area = 0.5 * (w_[..., 1:] + w_[..., :-1]) * (c_[..., 1:] - c_[..., :-1])

        cdf = torch.cat([torch.zeros_like(area[..., :1]), torch.cumsum(area, dim=-1)], dim=-1)

        # query piecewise quadratic interpolation
        cdf_interp = stepfun.sorted_interp_quad(cp, c_, w_, cdf)
        # difference between adjacent interpolated values
        w_s = torch.diff(cdf_interp, dim=-1)

        loss_anti_interlevel += ((w_s - wp).clamp_min(0)**2 / (wp + 1e-5)).mean()
    return config.anti_interlevel_loss_mult * loss_anti_interlevel


def distortion_loss(ray_history, config: configs.Config):
    """Computes the distortion loss regularizer defined in mip-NeRF 360."""
    last_ray_results = ray_history[-1]
    c = last_ray_results['sdist']
    w = last_ray_results['weights']
    loss = stepfun.lossfun_distortion(c, w).mean()
    return config.distortion_loss_mult * loss


def opacity_reg_loss(renderings, config: configs.Config):
    total_loss = 0.
    for rendering in renderings:
        o = rendering['acc']
        total_loss += config.opacity_loss_mult * (-o * torch.log(o + 1e-5)).mean()
    return total_loss


def hash_decay_loss(ray_history, config: configs.Config):
    last_ray_results = ray_history[-1]
    total_loss = torch.tensor(0., device=last_ray_results['sdist'].device)
    for ray_results in ray_history:
        if 'hash_levelwise_mean' not in ray_results:
            continue
        hash_levelwise_mean = ray_results['hash_levelwise_mean'].mean()
        total_loss += config.hash_decay_mult * hash_levelwise_mean
    return total_loss


def error_loss(batch, renderings, ray_history, config: configs.Config):
    rendering = renderings[-1]
    ray_history = ray_history[-1]
    residual = rendering['rgb'].detach() - batch['rgb'][..., :3]
    residual_sq = torch.square(residual)
    residual_target = residual_sq.sum(-1, keepdim=True)

    error_residual = rendering['error'] - torch.clamp(residual_target, 0.0, 1.0)
    error_residual = torch.where(error_residual > 0.0, error_residual,
                                 -config.error_loss_lower_lambda * error_residual)
    
    density = ray_history['error_density']
    rgb = ray_history['error_rgb']
    error_reg = 0.01 * density + 0.1 * rgb.mean(-1)

    loss = config.error_loss_mult * (error_residual.mean() + error_reg.mean())
    return loss


def density_reg_loss(model, config: configs.Config):
    total_loss = 0.
    stroke_step = model.nerf.stroke_step.item()
    density_alpha = model.nerf.density_params[:stroke_step]
    # Encourage the density alpha to be close to 0.
    loss = config.density_reg_loss_mult * density_alpha.mean()
    return loss


def transmittance_loss(rendering, config: configs.Config):
    rendering = rendering[-1]
    T = 1.0 - rendering['acc']
    loss = -torch.clamp(T.mean(), max=config.transmittance_target)
    return config.transmittance_loss_mult * loss


def entropy_loss(ray_history, config: configs.Config):
    ray_history = ray_history[-1]
    alphas = ray_history['weights'].clamp(1e-5, 1 - 1e-5)
    loss_entropy = (- alphas * torch.log2(alphas) - (1 - alphas) * torch.log2(1 - alphas)).mean()
    return config.entropy_loss_mult * loss_entropy


class StyleLoss(nn.Module):
    def __init__(self, device, config):
        super().__init__()
        vgg = torchvision.models.vgg16(pretrained=True).to(device).eval()
        for i, layer in enumerate(vgg.features):
            if isinstance(layer, torch.nn.MaxPool2d):
                vgg.features[i] = torch.nn.AvgPool2d(kernel_size=2, stride=2, padding=0)
                
        blocks = [vgg.features[:4], vgg.features[4:9]]
        if config.style_transfer_shape:
            blocks.append(vgg.features[9:16])
            blocks.append(vgg.features[16:23])
            
        for bl in blocks:
            for p in bl:
                p.requires_grad = False
        self.blocks = torch.nn.ModuleList(blocks)
        self.mean = torch.tensor([0.485, 0.456, 0.406], device=device).view(1, 3, 1, 1)
        self.std = torch.tensor([0.229, 0.224, 0.225], device=device).view(1, 3, 1, 1)
        self.loss_mult = config.style_loss_mult

        target_image = misc.load_img(config.style_target_image) / 255.
        target_image = torch.from_numpy(target_image).permute(2, 0, 1).unsqueeze(0).to(device)
        self.gram_targets = self.get_gram_matrices(target_image, detach=True)
        print(f'Loaded style image: {config.style_target_image}')
    
    def get_gram_matrices(self, image, detach=False):
        image = (image - self.mean) / self.std
        if image.shape[-2:] != (224, 224):
            image = nn.functional.interpolate(image, mode='bilinear', size=(224, 224), align_corners=False)
            
        gram_matrices = []
        x = image
        for block in self.blocks:
            x = block(x)
            b, ch, h, w = x.shape
            f = x.view(b, ch, w * h)
            f_t = f.transpose(1, 2)
            gram = torch.bmm(f, f_t) / (ch * w * h)
            if detach:
                gram = gram.detach()
            gram_matrices.append(gram)
        return gram_matrices
    
    def forward(self, output, target=None):
        gram_output = self.get_gram_matrices(output)
        if target is not None:
            gram_target = self.get_gram_matrices(target)
        else:
            gram_target = self.gram_targets
        
        loss = 0.0
        for gm_output, gm_target in zip(gram_output, gram_target):
            loss += torch.square(gm_output - gm_target).sum([1, 2]).mean()
        return loss * self.loss_mult
        

class CLIPLoss(nn.Module):
    def __init__(self, device, config):
        super().__init__()
        import clip
        self.model, self.preprocess = clip.load("ViT-B/32", device=device, jit=False)
        for p in self.model.parameters():
            p.requires_grad = False
        
        positive_prompts = config.clip_positive_prompt.split(",")
        self.use_direction_prompt = config.clip_use_direction_prompt
        if self.use_direction_prompt:
            self.positive_text_features = {}
            for key in [('front', 0, 90), ('side', 90, 90), ('back', 180, 90)]:
                direction, azimuth_target, azimuth_range = key
                positive_text = clip.tokenize([f'{prompt}, {direction} view' for prompt in positive_prompts]).to(device)
                text_features = self.model.encode_text(positive_text)
                self.positive_text_features[key] = text_features / text_features.norm(dim=-1, keepdim=True)
        else:
            positive_text = clip.tokenize(positive_prompts).to(device)
            text_features = self.model.encode_text(positive_text)
            self.positive_text_features = text_features / text_features.norm(dim=-1, keepdim=True)
            
        silhouette_text = clip.tokenize([f'silhouette mask of {prompt}' for prompt in positive_prompts]).to(device)
        text_features = self.model.encode_text(silhouette_text)
        self.silhouette_text_features = text_features / text_features.norm(dim=-1, keepdim=True)
            
        if config.clip_negative_prompt:
            negative_prompts = config.clip_negative_prompt.split(",")
            negative_text = clip.tokenize(negative_prompts).to(device)
            text_features = self.model.encode_text(negative_text)
            self.negative_text_features = text_features / text_features.norm(dim=-1, keepdim=True)
        else:
            self.negative_text_features = None
                
        self.transform = torchvision.transforms.Compose([
            # torchvision.transforms.RandomPerspective(fill=1, p=1, distortion_scale=0.5),
            # torchvision.transforms.RandomResizedCrop(224, scale=(0.7,0.99), antialias=True),
            torchvision.transforms.Resize(224, antialias=True),
            torchvision.transforms.Normalize((0.48145466, 0.4578275, 0.40821073), (0.26862954, 0.26130258, 0.27577711)),
        ])
        self.loss_mult = config.clip_loss_mult
        self.silhouette_mult = config.clip_silhouette_mult
        self.negative_mult = config.clip_negative_mult
        self.num_augs = 1
        
    def get_text_features(self, azimuth):
        if self.use_direction_prompt:
            pos_feats = 0
            weight_sum = 0
            for (direction, azimuth_target, azimuth_range), feats in self.positive_text_features.items():
                weight = torch.clamp(1 - torch.abs(azimuth - azimuth_target) / azimuth_range, 0, 1)
                pos_feats += weight[:, None, None] * feats[None, :, :]
                weight_sum += weight[:, None, None]
            pos_feats /= (weight_sum + 1e-8)
            guidance_mult = 1.0
        else:
            pos_feats = self.positive_text_features.unsqueeze(0)
            guidance_mult = 5 - 4 * torch.abs(azimuth) / 180
            guidance_mult /= guidance_mult.detach()  # normalize loss
            
        if self.negative_text_features is not None:
            neg_feats = self.negative_text_features.unsqueeze(0)
        else:
            neg_feats = None
        return pos_feats, neg_feats, guidance_mult
        
    def forward(self, batch, renderings):
        image = renderings[-1]['rgb'].permute(0, 3, 1, 2)
        silhouette = renderings[-1]['acc'].permute(0, 1, 2).unsqueeze(1).expand(-1, 3, -1, -1)
        azimuth = batch['azimuth'][:, 0, 0, 0]
        pos_feats, neg_feats, guidance_mult = self.get_text_features(azimuth)
        
        loss = 0.0
        for i in range(self.num_augs):
            image_features = self.model.encode_image(self.transform(image))
            image_features = image_features / image_features.norm(dim=-1, keepdim=True)
            
            pos_similarity = (image_features.unsqueeze(1) * pos_feats).sum(-1)
            loss += -(pos_similarity.mean(-1) * guidance_mult).mean()
            
            if self.silhouette_mult > 0:
                silhouette_features = self.model.encode_image(self.transform(silhouette))
                silhouette_features = silhouette_features / silhouette_features.norm(dim=-1, keepdim=True)
                sil_similarity = (silhouette_features.unsqueeze(1) * self.silhouette_text_features.unsqueeze(0)).sum(-1)
                loss += -self.silhouette_mult * (sil_similarity.mean(-1) * guidance_mult).mean()
            
            if neg_feats is not None:
                neg_similarity = (image_features.unsqueeze(1) * neg_feats).sum(-1)
                loss += self.negative_mult * (neg_similarity.mean(-1) * guidance_mult).mean()
                
        return loss / self.num_augs * self.loss_mult
            
            
class DiffusionLoss(nn.Module):
    def __init__(self, device, config):
        super().__init__()
        import transformers
        from diffusers import DDIMScheduler, StableDiffusionPipeline
        
        # suppress partial model loading warning
        transformers.logging.set_verbosity_error()
        
        model_key = "stabilityai/stable-diffusion-2-1-base"
        self.device = device
        self.precision_t = torch.float16 if config.diffusion_model_use_fp16 else torch.float32
        self.loss_mult = config.diffusion_loss_mult
        self.guidance_scale = 100
        
        # Create model
        pipe = StableDiffusionPipeline.from_pretrained(model_key, torch_dtype=self.precision_t)
        pipe = pipe.to(self.device)
        # pipe.enable_sequential_cpu_offload()
        # pipe.enable_vae_slicing()
        # pipe.unet.to(memory_format=torch.channels_last)
        # pipe.enable_attention_slicing(1)
        
        self.vae = pipe.vae
        self.tokenizer = pipe.tokenizer
        self.text_encoder = pipe.text_encoder
        self.unet = pipe.unet
        self.scheduler = DDIMScheduler.from_pretrained(model_key, subfolder="scheduler", torch_dtype=self.precision_t)
        del pipe
        
        self.num_train_timesteps = self.scheduler.config.num_train_timesteps
        self.min_step = int(self.num_train_timesteps * config.diffusion_t_range[0])
        self.max_step = int(self.num_train_timesteps * config.diffusion_t_range[1])
        self.alphas = self.scheduler.alphas_cumprod.to(device) # for convenience
        self.transform = torchvision.transforms.Compose([
            torchvision.transforms.Resize(224, antialias=True),
        ])
        
        positive_prompts = config.diffusion_positive_prompt.split(",")
        negative_prompts = config.diffusion_negative_prompt.split(",")
        self.positive_text_embeds = self.get_text_embeds(positive_prompts)
        self.negative_text_embeds = self.get_text_embeds(negative_prompts)
        
        print(f'Loaded diffusion model: {model_key}')
        
    @torch.no_grad()
    def get_text_embeds(self, prompts):
        inputs = self.tokenizer(prompts, padding='max_length', max_length=self.tokenizer.model_max_length, return_tensors='pt')
        embeddings = self.text_encoder(inputs.input_ids.to(self.device))[0].mean(0, keepdim=True)
        return embeddings
    
    def decode_latents(self, latents):
        latents = 1 / self.vae.config.scaling_factor * latents
        imgs = self.vae.decode(latents).sample
        imgs = (imgs / 2 + 0.5).clamp(0, 1)
        return imgs

    def encode_imgs(self, imgs):
        imgs = 2 * imgs - 1
        posterior = self.vae.encode(imgs).latent_dist
        latents = posterior.sample() * self.vae.config.scaling_factor
        return latents
        
    def forward(self, image):
        image = image.to(self.precision_t)
        # interp to 512x512 to be fed into vae.
        image_512 = nn.functional.interpolate(image, (512, 512), mode='bilinear', align_corners=False)
        # encode image into latents with vae, requires grad!
        latents = self.encode_imgs(image_512)
        
        # timestep ~ U(0.02, 0.98) to avoid very high/low noise level
        t = torch.randint(self.min_step, self.max_step + 1, (latents.shape[0],), dtype=torch.long, device=self.device)
        
        # predict the noise residual with unet, NO grad!
        with torch.no_grad():
            # prepare text embeds
            text_embeds = torch.cat([self.positive_text_embeds.expand(latents.shape[0], -1, -1), 
                                     self.negative_text_embeds.expand(latents.shape[0], -1, -1)])
            
            # add noise
            noise = torch.randn_like(latents)
            latents_noisy = self.scheduler.add_noise(latents, noise, t)
            # pred noise
            latent_model_input = torch.cat([latents_noisy] * 2)
            tt = torch.cat([t] * 2)
            noise_pred = self.unet(latent_model_input, tt, encoder_hidden_states=text_embeds).sample

            # perform guidance (high scale from paper!)
            noise_pred_pos, noise_pred_neg = noise_pred.chunk(2)
            noise_pred = noise_pred_neg + self.guidance_scale * (noise_pred_pos - noise_pred_neg)
            
        # w(t), sigma_t^2
        w = (1 - self.alphas[t])
        grad = 1.0 * w[:, None, None, None] * (noise_pred - noise)
        grad = torch.nan_to_num(grad)
        
        targets = (latents - grad).detach()
        loss = nn.functional.mse_loss(latents.float(), targets, reduction='mean')
        
        return loss * self.loss_mult
        
        
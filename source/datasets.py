import os
import cv2
import abc
import json
import enum
import torch
import numpy as np
from tqdm import tqdm
from PIL import Image
from source.utils import camera as camera_utils
from source.utils import image as image_utils
from source.utils import misc
from source import configs

# This is ugly, but it works.
import sys

__file_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, f'{__file_dir}/pycolmap')
sys.path.insert(0, f'{__file_dir}/pycolmap/pycolmap')
import pycolmap


def load_dataset(split, config: configs.Config) -> "Dataset":
    dataset_dict = {
        'blender': Blender,
        'llff': LLFF,
        'zeroshot': Zeroshot,
    }
    return dataset_dict[config.dataset_loader](split, config.data_dir, config)


class DataSplit(enum.Enum):
    """Dataset split."""
    TRAIN = 'train'
    TEST = 'test'


class BatchingMethod(enum.Enum):
    """Draw rays randomly from a single image or all images, in each batch."""
    ALL_IMAGES = 'all_images'
    SINGLE_IMAGE = 'single_image'


class NeRFSceneManager(pycolmap.SceneManager):
    """COLMAP pose loader.

    Minor NeRF-specific extension to the third_party Python COLMAP loader:
    google3/third_party/py/pycolmap/scene_manager.py
    """
    def process(self):
        """Applies NeRF-specific postprocessing to the loaded pose data.

        Returns:
            a tuple [image_names, poses, pixtocam, distortion_params].
            image_names:  contains the only the basename of the images.
            poses: [N, 4, 4] array containing the camera to world matrices.
            pixtocam: [N, 3, 3] array containing the camera to pixel space matrices.
            distortion_params: mapping of distortion param name to distortion
                parameters. Cameras share intrinsics. Valid keys are k1, k2, p1 and p2.
        """

        self.load_cameras()
        self.load_images()
        # self.load_points3D()  # For now, we do not need the point cloud data.

        # Assume shared intrinsics between all cameras.
        cam = self.cameras[1]

        # Extract focal lengths and principal point parameters.
        fx, fy, cx, cy = cam.fx, cam.fy, cam.cx, cam.cy
        pixtocam = np.linalg.inv(camera_utils.intrinsic_matrix(fx, fy, cx, cy))

        # Extract extrinsic matrices in world-to-camera format.
        imdata = self.images
        w2c_mats = []
        bottom = np.array([0, 0, 0, 1]).reshape(1, 4)
        for k in imdata:
            im = imdata[k]
            rot = im.R()
            trans = im.tvec.reshape(3, 1)
            w2c = np.concatenate([np.concatenate([rot, trans], 1), bottom], axis=0)
            w2c_mats.append(w2c)
        w2c_mats = np.stack(w2c_mats, axis=0)

        # Convert extrinsics to camera-to-world.
        c2w_mats = np.linalg.inv(w2c_mats)
        poses = c2w_mats[:, :3, :4]

        # Image names from COLMAP. No need for permuting the poses according to
        # image names anymore.
        names = [imdata[k].name for k in imdata]

        # Switch from COLMAP (right, down, fwd) to NeRF (right, up, back) frame.
        poses = poses @ np.diag([1, -1, -1, 1])

        # Get distortion parameters.
        type_ = cam.camera_type

        if type_ == 0 or type_ == 'SIMPLE_PINHOLE':
            params = None
            camtype = camera_utils.ProjectionType.PERSPECTIVE

        elif type_ == 1 or type_ == 'PINHOLE':
            params = None
            camtype = camera_utils.ProjectionType.PERSPECTIVE

        if type_ == 2 or type_ == 'SIMPLE_RADIAL':
            params = {k: 0. for k in ['k1', 'k2', 'k3', 'p1', 'p2']}
            params['k1'] = cam.k1
            camtype = camera_utils.ProjectionType.PERSPECTIVE

        elif type_ == 3 or type_ == 'RADIAL':
            params = {k: 0. for k in ['k1', 'k2', 'k3', 'p1', 'p2']}
            params['k1'] = cam.k1
            params['k2'] = cam.k2
            camtype = camera_utils.ProjectionType.PERSPECTIVE

        elif type_ == 4 or type_ == 'OPENCV':
            params = {k: 0. for k in ['k1', 'k2', 'k3', 'p1', 'p2']}
            params['k1'] = cam.k1
            params['k2'] = cam.k2
            params['p1'] = cam.p1
            params['p2'] = cam.p2
            camtype = camera_utils.ProjectionType.PERSPECTIVE

        elif type_ == 5 or type_ == 'OPENCV_FISHEYE':
            params = {k: 0. for k in ['k1', 'k2', 'k3', 'k4']}
            params['k1'] = cam.k1
            params['k2'] = cam.k2
            params['k3'] = cam.k3
            params['k4'] = cam.k4
            camtype = camera_utils.ProjectionType.FISHEYE

        return names, poses, pixtocam, params, camtype


class Dataset(torch.utils.data.Dataset):
    """Base class for a NeRF dataset.
    
    Creates batches of ray and color data used for training or rendering a NeRF model.

    Each subclass is responsible for loading images and camera poses from disk by
    implementing the _load_renderings() method. This data is used to generate
    train and test batches of ray + color data for feeding through the NeRF model.
    The ray parameters are calculated in _generate_rays().

    The public interface mimics the behavior of a standard machine learning
    pipeline dataset provider that can provide infinite batches of data to the
    training/testing pipelines without exposing any details of how the batches are
    loaded/created or how this is parallelized. Therefore, the initializer runs
    all setup, including data loading from disk using _load_renderings(), and
    begins the thread using its parent start() method. After the initializer
    returns, the caller can request batches of data straight away.

    The internal self._queue is initialized as queue.Queue(3), so the infinite
    loop in run() will block on the call self._queue.put(self._next_fn()) once
    there are 3 elements. The main thread training job runs in a loop that pops 1
    element at a time off the front of the queue. The Dataset thread's run() loop
    will populate the queue with 3 elements, then wait until a batch has been
    removed and push one more onto the end.

    This repeats indefinitely until the main thread's training loop completes
    (typically hundreds of thousands of iterations), then the main thread will
    exit and the Dataset thread will automatically be killed since it is a daemon.

    Attributes:
        alphas: np.ndarray, optional array of alpha channel data.
        cameras: tuple summarizing all camera extrinsic/intrinsic/distortion params.
        camtoworlds: np.ndarray, a list of extrinsic camera pose matrices.
        camtype: camera_utils.ProjectionType, fisheye or perspective camera.
        data_dir: str, location of the dataset on disk.
        distortion_params: dict, the camera distortion model parameters.
        far: float, far plane value for rays.
        focal: float, focal length from camera intrinsics.
        height: int, height of images.
        images: np.ndarray, array of RGB image data.
        near: float, near plane value for rays.
        pixtocams: np.ndarray, one or a list of inverse intrinsic camera matrices.
        pixtocam_ndc: np.ndarray, the inverse intrinsic matrix used for NDC space.
        poses: np.ndarray, optional array of auxiliary camera pose data.
        rays: utils.Rays, ray data for every pixel in the dataset.
        render_path: bool, indicates if a smooth camera path should be generated.
        size: int, number of images in the dataset.
        split: str, indicates if this is a "train" or "test" dataset.
        width: int, width of images.
    """
    def __init__(self, split: str, data_dir: str, config: configs.Config):
        super().__init__()

        # Initialize attributes
        self._patch_size = max(config.patch_size, 1)
        self._batch_size = config.batch_size // config.world_size
        if self._patch_size**2 > self._batch_size:
            raise ValueError(f'Patch size {self._patch_size}^2 too large for ' +
                             f'per-process batch size {self._batch_size}')
        self._batching = BatchingMethod(config.batching)
        self._render_spherical = False

        self.config = config
        self.global_rank = config.global_rank
        self.world_size = config.world_size
        self.split = DataSplit(split)
        self.data_dir = data_dir
        self.near = config.near
        self.far = config.far
        self.render_path = config.render_path
        self.distortion_params = None
        self.alphas = None
        self.poses = None
        self.pixtocam_ndc = None
        self.camtype = camera_utils.ProjectionType.PERSPECTIVE

        # Providing type comments for these attributes, they must be correctly
        # initialized by _load_renderings() (see docstring) in any subclass.
        self.images: np.ndarray = None
        self.camtoworlds: np.ndarray = None
        self.pixtocams: np.ndarray = None
        self.height: int = None
        self.width: int = None
        
        # Viewpoint information for zeroshot generation
        self.polar: np.ndarray = None
        self.azimuth: np.ndarray = None

        # Load data from disk using provided config parameters.
        self._load_renderings(config)

        if self.render_path:
            if config.render_path_file is not None:
                with misc.open_file(config.render_path_file, 'rb') as fp:
                    render_poses = np.load(fp)
                self.camtoworlds = render_poses
            if config.render_resolution is not None:
                self.width, self.height = config.render_resolution
            if config.render_focal is not None:
                self.focal = config.render_focal
            if config.render_camtype is not None:
                if config.render_camtype == 'pano':
                    self._render_spherical = True
                else:
                    self.camtype = camera_utils.ProjectionType(config.render_camtype)

            self.distortion_params = None
            self.pixtocams = camera_utils.get_pixtocam(self.focal, self.width, self.height)

        self._n_examples = self.camtoworlds.shape[0]

        self.cameras = (self.pixtocams, self.camtoworlds, self.distortion_params, self.pixtocam_ndc)

        # Seed the queue with one batch to avoid race condition.
        if self.split == DataSplit.TRAIN:
            self._next_fn = self._next_train
        else:
            self._next_fn = self._next_test

    @property
    def size(self):
        return self._n_examples

    def __len__(self):
        if self.split == DataSplit.TRAIN:
            return 1000
        else:
            return self._n_examples

    @abc.abstractmethod
    def _load_renderings(self, config):
        """Load images and poses from disk.

        Args:
            config: utils.Config, user-specified config parameters.
        In inherited classes, this method must set the following public attributes:
            images: [N, height, width, 3] array for RGB images.
            camtoworlds: [N, 3, 4] array of extrinsic pose matrices.
            poses: [..., 3, 4] array of auxiliary pose data (optional).
            pixtocams: [N, 3, 4] array of inverse intrinsic matrices.
            distortion_params: dict, camera lens distortion model parameters.
            height: int, height of images.
            width: int, width of images.
            focal: float, focal length to use for ideal pinhole rendering.
        """

    def _make_ray_batch(self, pix_x_int, pix_y_int, cam_idx):
        """Creates ray data batch from pixel coordinates and camera indices.

            All arguments must have broadcastable shapes. If the arguments together
            broadcast to a shape [a, b, c, ..., z] then the returned utils.Rays object
            will have array attributes with shape [a, b, c, ..., z, N], where N=3 for
            3D vectors and N=1 for per-ray scalar attributes.

            Args:
              pix_x_int: int array, x coordinates of image pixels.
              pix_y_int: int array, y coordinates of image pixels.
              cam_idx: int or int array, camera indices.

            Returns:
              A dict mapping from strings utils.Rays or arrays of image data.
              This is the batch provided for one NeRF train or test iteration.
        """

        broadcast_scalar = lambda x: np.broadcast_to(x, pix_x_int.shape)[..., None]
        ray_kwargs = {
            'near': broadcast_scalar(self.near),
            'far': broadcast_scalar(self.far),
            'cam_idx': broadcast_scalar(cam_idx),
        }
        # Collect per-camera information needed for each ray.
        pixels = dict(pix_x_int=pix_x_int, pix_y_int=pix_y_int, **ray_kwargs)

        # Slow path, do ray computation using numpy (on CPU).
        batch = camera_utils.cast_ray_batch(self.cameras, pixels, self.camtype)

        if not self.render_path:
            if self.images is not None:
                batch['rgb'] = self.images[cam_idx, pix_y_int, pix_x_int]
            else:
                empty_image = np.ones((self.height, self.width, 3), dtype=np.float32)
                batch['rgb'] = np.tile(empty_image[pix_y_int, pix_x_int], cam_idx.shape + (1, 1, 1))
            if self.alphas is not None:
                batch['alphas'] = self.alphas[cam_idx, pix_y_int, pix_x_int]
            if self.polar is not None:
                batch['polar'] = self.polar[ray_kwargs['cam_idx']]
            if self.azimuth is not None:
                batch['azimuth'] = self.azimuth[ray_kwargs['cam_idx']]
        return {
            k: torch.from_numpy(v.copy()).float() if v is not None else None
            for k, v in batch.items()
        }

    def _next_train(self, item):
        """Sample next training batch (random rays)."""
        # We assume all images in the dataset are the same resolution, so we can use
        # the same width/height for sampling all pixels coordinates in the batch.
        # Batch/patch sampling parameters.
        num_patches = self._batch_size // self._patch_size**2
        upper_border = self._patch_size - 1
        # Random pixel patch x-coordinates.
        pix_x_int = np.random.randint(0, self.width - upper_border, (num_patches, 1, 1))
        # Random pixel patch y-coordinates.
        pix_y_int = np.random.randint(0, self.height - upper_border, (num_patches, 1, 1))
        # Add patch coordinate offsets.
        # Shape will broadcast to (num_patches, _patch_size, _patch_size).
        patch_dx_int, patch_dy_int = camera_utils.pixel_coordinates(self._patch_size,
                                                                    self._patch_size)
        pix_x_int = pix_x_int + patch_dx_int
        pix_y_int = pix_y_int + patch_dy_int
        # Random camera indices.
        if self._batching == BatchingMethod.ALL_IMAGES:
            cam_idx = np.random.randint(0, self._n_examples, (num_patches, 1, 1))
        else:
            cam_idx = np.random.randint(0, self._n_examples, (1, ))

        return self._make_ray_batch(pix_x_int, pix_y_int, cam_idx)

    def generate_ray_batch(self, cam_idx: int):
        """Generate ray batch for a specified camera in the dataset."""
        if self._render_spherical:
            camtoworld = self.camtoworlds[cam_idx]
            rays = camera_utils.cast_spherical_rays(camtoworld, self.height, self.width, self.near,
                                                    self.far)
            return rays
        else:
            # Generate rays for all pixels in the image.
            pix_x_int, pix_y_int = camera_utils.pixel_coordinates(self.width, self.height)
            return self._make_ray_batch(pix_x_int, pix_y_int, cam_idx)

    def _next_test(self, item):
        """Sample next test batch (one full image)."""
        return self.generate_ray_batch(item)

    def collate_fn(self, item):
        return self._next_fn(item[0])

    def __getitem__(self, item):
        return self._next_fn(item)


class Blender(Dataset):
    """Blender Dataset."""
    def _load_renderings(self, config):
        """Load images from disk."""
        if config.render_path:
            raise ValueError('render_path cannot be used for the blender dataset.')
        pose_file = os.path.join(self.data_dir, f'transforms_{self.split.value}.json')
        with misc.open_file(pose_file, 'r') as fp:
            meta = json.load(fp)
        images = []
        cams = []
        for idx, frame in enumerate(
                tqdm(meta['frames'],
                     desc='Loading Blender dataset',
                     disable=self.global_rank != 0,
                     leave=False)):
            fprefix = os.path.join(self.data_dir, frame['file_path'])

            def get_img(f, fprefix=fprefix):
                image = misc.load_img(fprefix + f)
                if config.factor > 1:
                    image = image_utils.downsample(image, config.factor)
                return image

            if config.use_tiffs:
                channels = [get_img(f'_{ch}.tiff') for ch in ['R', 'G', 'B', 'A']]
                # Convert image to sRGB color space.
                image = image_utils.linear_to_srgb_np(np.stack(channels, axis=-1))
            else:
                image = get_img('.png') / 255.
            images.append(image)

            cams.append(np.array(frame['transform_matrix'], dtype=np.float32))

        self.images = np.stack(images, axis=0)

        rgb, alpha = self.images[..., :3], self.images[..., -1:]
        self.images = rgb * alpha + (1. - alpha)  # Use a white background.
        self.alphas = alpha[..., 0]  # Load alpha mask.
        self.height, self.width = self.images.shape[1:3]
        self.camtoworlds = np.stack(cams, axis=0)
        self.focal = .5 * self.width / np.tan(.5 * float(meta['camera_angle_x']))
        self.pixtocams = camera_utils.get_pixtocam(self.focal, self.width, self.height)


class LLFF(Dataset):
    """LLFF Dataset."""
    def _load_renderings(self, config):
        """Load images from disk."""
        # Set up scaling factor.
        image_dir_suffix = ''
        # Use downsampling factor.
        if config.factor > 0:
            image_dir_suffix = f'_{config.factor}'
            factor = config.factor
        else:
            factor = 1

        # Copy COLMAP data to local disk for faster loading.
        colmap_dir = os.path.join(self.data_dir, 'sparse/0/')

        # Load poses.
        if misc.file_exists(colmap_dir):
            pose_data = NeRFSceneManager(colmap_dir).process()
        else:
            # # Attempt to load Blender/NGP format if COLMAP data not present.
            # pose_data = load_blender_posedata(self.data_dir)
            raise ValueError('COLMAP data not found.')
        image_names, poses, pixtocam, distortion_params, camtype = pose_data

        # Previous NeRF results were generated with images sorted by filename,
        # use this flag to ensure metrics are reported on the same test set.
        if config.load_alphabetical:
            inds = np.argsort(image_names)
            image_names = [image_names[i] for i in inds]
            poses = poses[inds]

        # Load bounds if possible (only used in forward facing scenes).
        posefile = os.path.join(self.data_dir, 'poses_bounds.npy')
        if misc.file_exists(posefile):
            with misc.open_file(posefile, 'rb') as fp:
                poses_arr = np.load(fp)
            bounds = poses_arr[:, -2:]
        else:
            bounds = np.array([0.01, 1.])
        self.colmap_to_world_transform = np.eye(4)

        # Scale the inverse intrinsics matrix by the image downsampling factor.
        pixtocam = pixtocam @ np.diag([factor, factor, 1.])
        self.pixtocams = pixtocam.astype(np.float32)
        self.focal = 1. / self.pixtocams[0, 0]
        self.distortion_params = distortion_params
        self.camtype = camtype

        # Separate out 360 versus forward facing scenes.
        if config.forward_facing:
            # Set the projective matrix defining the NDC transformation.
            self.pixtocam_ndc = self.pixtocams.reshape(-1, 3, 3)[0]
            # Rescale according to a default bd factor.
            scale = 1. / (bounds.min() * .75)
            poses[:, :3, 3] *= scale
            self.colmap_to_world_transform = np.diag([scale] * 3 + [1])
            bounds *= scale
            # Recenter poses.
            poses, transform = camera_utils.recenter_poses(poses)
            self.colmap_to_world_transform = (transform @ self.colmap_to_world_transform)
            # Forward-facing spiral render path.
            self.render_poses = camera_utils.generate_spiral_path(
                poses, bounds, n_frames=config.render_path_frames)
        else:
            # Rotate/scale poses to align ground with xy plane and fit to unit cube.
            poses, transform = camera_utils.transform_poses_pca(poses)
            self.colmap_to_world_transform = transform
            if config.render_spline_keyframes is not None:
                rets = camera_utils.create_render_spline_path(config, image_names, poses,
                                                              self.exposures)
                self.spline_indices, self.render_poses, self.render_exposures = rets
            else:
                # Automatically generated inward-facing elliptical render path.
                self.render_poses = camera_utils.generate_ellipse_path(
                    poses,
                    n_frames=config.render_path_frames,
                    z_variation=config.z_variation,
                    z_phase=config.z_phase)

        # Select the split.
        all_indices = np.arange(len(image_names))
        if config.llff_use_all_images_for_training:
            train_indices = all_indices
        else:
            train_indices = all_indices % config.llffhold != 0
        if config.llff_use_all_images_for_testing:
            test_indices = all_indices
        else:
            test_indices = all_indices % config.llffhold == 0
        split_indices = {
            DataSplit.TEST: all_indices[test_indices],
            DataSplit.TRAIN: all_indices[train_indices],
        }
        indices = split_indices[self.split]
        image_names = [image_names[i] for i in indices]
        poses = poses[indices]

        # Load images.
        colmap_image_dir = os.path.join(self.data_dir, 'images')
        image_dir = os.path.join(self.data_dir, 'images' + image_dir_suffix)
        for d in [image_dir, colmap_image_dir]:
            if not misc.file_exists(d):
                raise ValueError(f'Image folder {d} does not exist.')
        # Downsampled images may have different names vs images used for COLMAP,
        # so we need to map between the two sorted lists of files.
        colmap_files = sorted(misc.listdir(colmap_image_dir))
        image_files = sorted(misc.listdir(image_dir))
        colmap_to_image = dict(zip(colmap_files, image_files))
        image_paths = [os.path.join(image_dir, colmap_to_image[f]) for f in image_names]
        images = [
            misc.load_img(x) for x in tqdm(image_paths,
                                           desc='Loading LLFF dataset',
                                           disable=self.global_rank != 0,
                                           leave=False)
        ]
        images = np.stack(images, axis=0) / 255.

        self.poses = poses
        self.images = images
        self.camtoworlds = self.render_poses if config.render_path else poses
        self.height, self.width = images.shape[1:3]


class TanksAndTemplesNerfPP(Dataset):
    """Subset of Tanks and Temples Dataset as processed by NeRF++."""
    def _load_renderings(self, config):
        """Load images from disk."""
        if config.render_path:
            split_str = 'camera_path'
        else:
            split_str = self.split.value

        basedir = os.path.join(self.data_dir, split_str)

        def load_files(dirname, load_fn, shape=None):
            files = [
                os.path.join(basedir, dirname, f)
                for f in sorted(misc.listdir(os.path.join(basedir, dirname)))
            ]
            mats = np.array([load_fn(misc.open_file(f, 'rb')) for f in files])
            if shape is not None:
                mats = mats.reshape(mats.shape[:1] + shape)
            return mats

        poses = load_files('pose', np.loadtxt, (4, 4))
        # Flip Y and Z axes to get correct coordinate frame.
        poses = np.matmul(poses, np.diag(np.array([1, -1, -1, 1])))

        # For now, ignore all but the first focal length in intrinsics
        intrinsics = load_files('intrinsics', np.loadtxt, (4, 4))

        if not config.render_path:
            images = load_files('rgb', lambda f: np.array(Image.open(f))) / 255.
            self.images = images
            self.height, self.width = self.images.shape[1:3]

        else:
            # Hack to grab the image resolution from a test image
            d = os.path.join(self.data_dir, 'test', 'rgb')
            f = os.path.join(d, sorted(misc.listdir(d))[0])
            shape = misc.load_img(f).shape
            self.height, self.width = shape[:2]
            self.images = None

        self.camtoworlds = poses
        self.focal = intrinsics[0, 0, 0]
        self.pixtocams = camera_utils.get_pixtocam(self.focal, self.width, self.height)


class TanksAndTemplesFVS(Dataset):
    """Subset of Tanks and Temples Dataset as processed by Free View Synthesis."""
    def _load_renderings(self, config):
        """Load images from disk."""
        render_only = config.render_path and self.split == DataSplit.TEST

        basedir = os.path.join(self.data_dir, 'dense')
        sizes = [f for f in sorted(misc.listdir(basedir)) if f.startswith('ibr3d')]
        sizes = sizes[::-1]

        if config.factor >= len(sizes):
            raise ValueError(f'Factor {config.factor} larger than {len(sizes)}')

        basedir = os.path.join(basedir, sizes[config.factor])
        open_fn = lambda f: misc.open_file(os.path.join(basedir, f), 'rb')

        files = [f for f in sorted(misc.listdir(basedir)) if f.startswith('im_')]
        if render_only:
            files = files[:1]
        images = np.array([np.array(Image.open(open_fn(f))) for f in files]) / 255.

        names = ['Ks', 'Rs', 'ts']
        intrinsics, rot, trans = (np.load(open_fn(f'{n}.npy')) for n in names)

        # Convert poses from colmap world-to-cam into our cam-to-world.
        w2c = np.concatenate([rot, trans[..., None]], axis=-1)
        c2w_colmap = np.linalg.inv(camera_utils.pad_poses(w2c))[:, :3, :4]
        c2w = c2w_colmap @ np.diag(np.array([1, -1, -1, 1]))

        # Reorient poses so z-axis is up
        poses, _ = camera_utils.transform_poses_pca(c2w)
        self.poses = poses

        self.images = images
        self.height, self.width = self.images.shape[1:3]
        self.camtoworlds = poses
        # For now, ignore all but the first focal length in intrinsics
        self.focal = intrinsics[0, 0, 0]
        self.pixtocams = camera_utils.get_pixtocam(self.focal, self.width, self.height)

        if render_only:
            render_path = camera_utils.generate_ellipse_path(poses,
                                                             config.render_path_frames,
                                                             z_variation=config.z_variation,
                                                             z_phase=config.z_phase)
            self.images = None
            self.camtoworlds = render_path
            self.render_poses = render_path
        else:
            # Select the split.
            all_indices = np.arange(images.shape[0])
            indices = {
                DataSplit.TEST: all_indices[all_indices % config.llffhold == 0],
                DataSplit.TRAIN: all_indices[all_indices % config.llffhold != 0],
            }[self.split]

            self.images = self.images[indices]
            self.camtoworlds = self.camtoworlds[indices]


class DTU(Dataset):
    """DTU Dataset."""
    def _load_renderings(self, config):
        """Load images from disk."""
        if config.render_path:
            raise ValueError('render_path cannot be used for the DTU dataset.')

        images = []
        pixtocams = []
        camtoworlds = []

        # Find out whether the particular scan has 49 or 65 images.
        n_images = len(misc.listdir(self.data_dir)) // 8

        # Loop over all images.
        for i in range(1, n_images + 1):
            # Set light condition string accordingly.
            if config.dtu_light_cond < 7:
                light_str = f'{config.dtu_light_cond}_r' + ('5000' if i < 50 else '7000')
            else:
                light_str = 'max'

            # Load image.
            fname = os.path.join(self.data_dir, f'rect_{i:03d}_{light_str}.png')
            image = misc.load_img(fname) / 255.
            if config.factor > 1:
                image = image_utils.downsample(image, config.factor)
            images.append(image)

            # Load projection matrix from file.
            fname = os.path.join(self.data_dir, f'../../cal18/pos_{i:03d}.txt')
            with misc.open_file(fname, 'rb') as f:
                projection = np.loadtxt(f, dtype=np.float32)

            # Decompose projection matrix into pose and camera matrix.
            camera_mat, rot_mat, t = cv2.decomposeProjectionMatrix(projection)[:3]
            camera_mat = camera_mat / camera_mat[2, 2]
            pose = np.eye(4, dtype=np.float32)
            pose[:3, :3] = rot_mat.transpose()
            pose[:3, 3] = (t[:3] / t[3])[:, 0]
            pose = pose[:3]
            camtoworlds.append(pose)

            if config.factor > 0:
                # Scale camera matrix according to downsampling factor.
                camera_mat = np.diag([1. / config.factor, 1. / config.factor, 1.]).astype(
                    np.float32) @ camera_mat
            pixtocams.append(np.linalg.inv(camera_mat))

        pixtocams = np.stack(pixtocams)
        camtoworlds = np.stack(camtoworlds)
        images = np.stack(images)

        def rescale_poses(poses):
            """Rescales camera poses according to maximum x/y/z value."""
            s = np.max(np.abs(poses[:, :3, -1]))
            out = np.copy(poses)
            out[:, :3, -1] /= s
            return out

        # Center and scale poses.
        camtoworlds, _ = camera_utils.recenter_poses(camtoworlds)
        camtoworlds = rescale_poses(camtoworlds)
        # Flip y and z axes to get poses in OpenGL coordinate system.
        camtoworlds = camtoworlds @ np.diag([1., -1., -1., 1.]).astype(np.float32)

        all_indices = np.arange(images.shape[0])
        split_indices = {
            DataSplit.TEST: all_indices[all_indices % config.dtuhold == 0],
            DataSplit.TRAIN: all_indices[all_indices % config.dtuhold != 0],
        }
        indices = split_indices[self.split]

        self.images = images[indices]
        self.height, self.width = images.shape[1:3]
        self.camtoworlds = camtoworlds[indices]
        self.pixtocams = pixtocams[indices]


class Zeroshot(Dataset):
    """Randomly sampled camera poses for zero-shot generation."""
    
    def _rand_poses(self, size, 
                    radius_range=[3.5, 4.5], theta_range=[-15, 50], phi_range=[0, 360], 
                    radius_normal_dist=False, theta_normal_dist=False, 
                    jitter_pose=True, jitter_center=0.015, jitter_target=0.015, jitter_up=0.01):
        """generate random poses from an orbit camera."""
        theta_range = np.deg2rad(np.array(theta_range))
        phi_range = np.deg2rad(np.array(phi_range))
        
        if radius_normal_dist:
            radius = misc.sample_normal_dist(size, radius_range[0], radius_range[1])
        else:
            radius = np.random.rand(size) * (radius_range[1] - radius_range[0]) + radius_range[0]
            
        if theta_normal_dist:
            thetas = misc.sample_normal_dist(size, theta_range[0], theta_range[1])
        else:
            thetas = np.random.rand(size) * (theta_range[1] - theta_range[0]) + theta_range[0]
            
        phis = np.random.rand(size) * (phi_range[1] - phi_range[0]) + phi_range[0]
        phis[phis < 0] += 2 * np.pi
        phis[phis > 2 * np.pi] -= 2 * np.pi

        centers = np.stack([
            radius * np.cos(thetas) * np.sin(phis),
            radius * np.cos(thetas) * np.cos(phis),
            radius * np.sin(thetas),
        ], axis=-1) # [N, 3]
        
        targets = 0
        if jitter_pose:
            centers += (np.random.rand(*centers.shape) - 0.5) * jitter_center
            targets = np.random.randn(*centers.shape) * jitter_target
            
        # lookat
        forward_vector = misc.safe_normalize(centers - targets)
        up_vector = np.tile(np.array([0, 0, 1]), (size, 1))
        right_vector = misc.safe_normalize(np.cross(forward_vector, up_vector))
        
        if jitter_pose:
            up_noise = np.random.randn(*up_vector.shape) * jitter_up
        else:
            up_noise = 0
        up_vector = misc.safe_normalize(np.cross(right_vector, forward_vector) + up_noise)
        
        poses = np.tile(np.eye(4), (size, 1, 1))
        poses[:, :3, :3] = np.stack((right_vector, up_vector, forward_vector), axis=-1)
        poses[:, :3, 3] = centers
        
        # back to degree
        thetas = np.rad2deg(thetas)
        phis = np.rad2deg(phis)
        
        return poses, thetas, phis, radius
        
    
    def _circle_poses(self, size, radius=4.0, theta_range=[0, 40], phi_range=[0, 360]):
        theta_range = np.deg2rad(np.array(theta_range))
        phi_range = np.deg2rad(np.array(phi_range))
        
        radius = np.ones(size) * radius
        thetas = np.linspace(theta_range[0], theta_range[1], size)
        phis = np.linspace(phi_range[0], phi_range[1], size)
        centers = np.stack([
            radius * np.cos(thetas) * np.sin(phis),
            radius * np.cos(thetas) * np.cos(phis),
            radius * np.sin(thetas),
        ], axis=-1) # [N, 3]
        
        # lookat
        forward_vector = misc.safe_normalize(centers)
        up_vector = np.tile(np.array([0, 0, 1]), (size, 1))
        right_vector = misc.safe_normalize(np.cross(forward_vector, up_vector))
        up_vector = misc.safe_normalize(np.cross(right_vector, forward_vector))
        
        poses = np.tile(np.eye(4), (size, 1, 1))
        poses[:, :3, :3] = np.stack((right_vector, up_vector, forward_vector), axis=-1)
        poses[:, :3, 3] = centers
        
        # back to degree
        thetas = np.rad2deg(thetas)
        phis = np.rad2deg(phis)
        
        return poses, thetas, phis, radius
    
    
    def _load_renderings(self, config):
        if config.render_path:
            raise ValueError('render_path cannot be used for the zeroshot dataset.')
        
        self.height, self.width = 800 // config.factor, 800 // config.factor
        
        if self.split == DataSplit.TRAIN:
            poses, thetas, phis, radius = self._rand_poses(500000)
        else:
            poses, thetas, phis, radius = self._circle_poses(100)
        self.camtoworlds = poses
        self.polar = thetas
        self.azimuth = phis
        self.azimuth[self.azimuth > 180] -= 360  # range in [-180, 180]
        
        # fixed focal
        fov = 40
        self.focal = .5 * self.width / np.tan(.5 * np.deg2rad(fov))
        self.pixtocams = camera_utils.get_pixtocam(self.focal, self.width, self.height)


if __name__ == '__main__':
    cfg = configs.Config()
    cfg.world_size = 1
    cfg.global_rank = 0
    dataset = Blender('test', 'data/nerf_synthetic/lego', cfg)
    print(dataset.size)
    for i, data in enumerate(tqdm(dataset)):
        if i > 1: continue
        print(f'sample {i} ' + '=' * 40)
        for k, v in data.items():
            if isinstance(v, torch.Tensor):
                print(f"  {k}: {v.dtype} {v.shape}")
                print(f"    mean={v.mean()} std={v.std()} range=[{v.min()},{v.max()}]")
            else:
                print(f"  {k}: {v}")
    print('done')
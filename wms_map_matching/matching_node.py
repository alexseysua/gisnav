import rclpy
import sys
import os
import traceback
import yaml
import importlib
import math

from enum import Enum
from rclpy.node import Node
from rcl_interfaces.msg import ParameterDescriptor
from owslib.wms import WebMapService
from cv2 import VideoCapture, imwrite, imdecode
import numpy as np
import cv2  # TODO: remove
from cv_bridge import CvBridge
from scipy.spatial.transform import Rotation
from functools import partial
from wms_map_matching.util import get_bbox, setup_sys_path, convert_fov_from_pix_to_wgs84,\
    write_fov_and_camera_location_to_geojson, get_bbox_center, BBox, Dimensions, rotate_and_crop_map, \
    visualize_homography, get_fov, get_camera_distance, get_distance_of_fov_center, LatLon, fov_to_bbox,\
    get_angle, create_src_corners, uncrop_pixel_coordinates, rotate_point, move_distance, RPY, LatLonAlt

# Add the share folder to Python path
share_dir, superglue_dir = setup_sys_path()  # TODO: Define superglue_dir elsewhere? just use this to get share_dir

# Import this after util.setup_sys_path has been called
from wms_map_matching.superglue import SuperGlue


class Matcher(Node):
    # scipy Rotations: {‘X’, ‘Y’, ‘Z’} for intrinsic, {‘x’, ‘y’, ‘z’} for extrinsic rotations
    EULER_SEQUENCE = 'YXZ'

    # Minimum matches for homography estimation, should be at least 4
    MINIMUM_MATCHES = 4

    class TopicType(Enum):
        """Enumerates microRTPS bridge topic types."""
        PUB = 1
        SUB = 2

    def __init__(self, share_directory, superglue_directory, config='config.yml'):
        """Initializes the node.

        Arguments:
            share_dir - String path of the share directory where configuration and other files are.
            superglue_dir - String path of the directory where SuperGlue related files are.
            config - String path to the config file in the share folder.
        """
        super().__init__('matcher')
        self.share_dir = share_directory  # TODO: make private?
        self.superglue_dir = superglue_directory  # TODO: move this to _setup_superglue? private _superglue_dir instead?
        self._load_config(config)
        self._init_wms()

        # Dict for storing all microRTPS bridge subscribers and publishers
        self._topics = dict()
        self._setup_topics()

        # Dict for storing latest microRTPS messages
        self._topics_msgs = dict()

        # Convert image_raw to cv2 compatible image and store it here
        self._cv_bridge = CvBridge()
        self._cv_image = None

        # Store map raster received from WMS endpoint here along with its bounding box
        self._map = None
        self._map_bbox = None

        self._setup_superglue()

        self._gimbal_fov_wgs84 = []  # TODO: remove this attribute, just passing it through here from _update_map to _match (temp hack)

    def _setup_superglue(self):
        """Sets up SuperGlue."""  # TODO: make all these private?
        self._superglue = SuperGlue(self._config['superglue'], self.get_logger())

    def _load_config(self, yaml_file):
        """Loads config from the provided YAML file."""
        with open(os.path.join(share_dir, yaml_file), 'r') as f:
            try:
                self._config = yaml.safe_load(f)
                self.get_logger().info('Loaded config:\n{}.'.format(self._config))
            except Exception as e:
                self.get_logger().error('Could not load config file {} because of exception: {}\n{}' \
                                        .format(yaml_file, e, traceback.print_exc()))

    def _use_gimbal_projection(self):
        """Returns True if gimbal projection is enabled for fetching map bbox rasters."""
        gimbal_projection_flag = self._config.get('misc', {}).get('gimbal_projection', False)
        if type(gimbal_projection_flag) is bool:
            return gimbal_projection_flag
        else:
            self.get_logger().warn(f'Could not read gimbal projection flag: {gimbal_projection_flag}. Assume False.')
            return False

    def _restrict_affine(self):
        """Returns True if homography matrix should be restricted to an affine transformation (nadir facing camera)."""
        restrict_affine_flag = self._config.get('misc', {}).get('affine', False)
        if type(restrict_affine_flag) is bool:
            return restrict_affine_flag
        else:
            self.get_logger().warn(f'Could not read affine restriction flag: {restrict_affine_flag}. Assume False.')
            return False

    def _import_class(self, class_name, module_name):
        """Imports class from module if not yet imported."""
        if module_name not in sys.modules:
            self.get_logger().info('Importing module ' + module_name + '.')
            importlib.import_module(module_name)
        imported_class = getattr(sys.modules[module_name], class_name, None)
        assert imported_class is not None, class_name + ' was not found in module ' + module_name + '.'
        return imported_class

    def _setup_topics(self):
        """Loads and sets up ROS2 publishers and subscribers from config file."""
        for topic_name, msg_type in self._config['ros2_topics']['sub'].items():
            module_name, msg_type = msg_type.rsplit('.', 1)
            msg_class = self._import_class(msg_type, module_name)
            self._init_topic(topic_name, self.TopicType.SUB, msg_class)

        for topic_name, msg_type in self._config['ros2_topics']['pub'].items():
            module_name, msg_type = msg_type.rsplit('.', 1)
            msg_class = self._import_class(msg_type, module_name)
            self._init_topic(topic_name, self.TopicType.PUB, msg_class)

        self.get_logger().info('Topics setup complete with keys: ' + str(self._topics.keys()))

    def _init_topic(self, topic_name, topic_type, msg_type):
        """Sets up rclpy publishers and subscribers and dynamically loads message types from px4_msgs library."""
        if topic_type is self.TopicType.PUB:
            self._topics[topic_name] = self.create_publisher(msg_type, topic_name, 10)
        elif topic_type is self.TopicType.SUB:
            callback_name = '_' + topic_name.lower() + '_callback'
            callback = getattr(self, callback_name, None)
            assert callback is not None, 'Missing callback implementation: ' + callback_name
            self._topics[topic_name] = self.create_subscription(msg_type, topic_name, callback, 10)
        else:
            raise TypeError('Unsupported topic type: {}'.format(topic_type))

    def _init_wms(self):
        """Initializes the Web Map Service (WMS) client used by the node to request map rasters.

        The url and version parameters are required to initialize the WMS client and are therefore set to read only. The
        layer and srs parameters can be changed dynamically.
        """
        self.declare_parameter('url', self._config['wms']['url'], ParameterDescriptor(read_only=True))
        self.declare_parameter('version', self._config['wms']['version'], ParameterDescriptor(read_only=True))
        self.declare_parameter('layer', self._config['wms']['layer'])
        self.declare_parameter('srs', self._config['wms']['srs'])

        try:
            self._wms = WebMapService(self.get_parameter('url').get_parameter_value().string_value,
                                      version=self.get_parameter('version').get_parameter_value().string_value)
        except Exception as e:
            self.get_logger().error('Could not connect to WMS server.')
            raise e

    def _map_size(self):
        max_dim = max(self._camera_info().width, self._camera_info().height)
        return max_dim, max_dim

    def _camera_info(self):
        """Returns camera info."""
        return self._get_simple_info('camera_info')

    def _get_simple_info(self, message_name):
        """Returns message received via microRTPS bridge or None if message was not yet received."""
        info = self._topics_msgs.get(message_name, None)
        if info is None:
            self.get_logger().warn(message_name + ' info not available.')
        return info

    def _global_position(self):
        return self._get_simple_info('VehicleGlobalPosition')

    def _map_size_with_padding(self):
        dim = self._img_dimensions()
        if type(dim) is not Dimensions:
            self.get_logger().warn(f'Dimensions not available - returning None as map size.')
            return None
        assert hasattr(dim, 'width') and hasattr(dim, 'height'), 'Dimensions did not have expected attributes.'
        diagonal = math.ceil(math.sqrt(dim.width ** 2 + dim.height ** 2))
        return diagonal, diagonal

    def _map_dimensions_with_padding(self):
        map_size = self._map_size_with_padding()
        if map_size is None:
            self.get_logger().warn(f'Map size with padding not available - returning None as map dimensions.')
            return None
        assert len(map_size) == 2, f'Map size was unexpected length {len(map_size)}, 2 expected.'
        return Dimensions(*map_size)

    def _declared_img_size(self):
        camera_info = self._camera_info()
        if camera_info is not None:
            assert hasattr(camera_info, 'height') and hasattr(camera_info, 'width'), \
                'Height or width info was unexpectedly not included in CameraInfo message.'
            return camera_info.height, camera_info.width  # numpy order: h, w, c --> height first
        else:
            self.get_logger().warn('Camera info was not available - returning None as declared image size.')
            return None

    def _img_dimensions(self):
        declared_size = self._declared_img_size()
        if declared_size is None:
            self.get_logger().warn('CDeclared size not available - returning None as image dimensions.')
            return None
        else:
            return Dimensions(*declared_size)

    def _vehicle_attitude(self):
        """Returns vehicle attitude from VehicleAttitude message."""
        return self._get_simple_info('VehicleAttitude')

    def _project_gimbal_fov(self, altitude_meters):
        """Returns field of view BBox projected using gimbal attitude and camera intrinsics information."""
        rpy = self._get_camera_rpy()
        if rpy is None:
            self.get_logger().warn('Could not get RPY - cannot project gimbal fov.')
            return

        r = Rotation.from_euler(self.EULER_SEQUENCE, list(rpy), degrees=True).as_matrix()
        e = np.hstack((r, np.expand_dims(np.array([0, 0, altitude_meters]), axis=1)))  # extrinsic matrix  # [0, 0, 1]
        assert e.shape == (3, 4), 'Extrinsic matrix had unexpected shape: ' + str(e.shape) \
                                  + ' - could not project gimbal FoV.'

        camera_info = self._camera_info()
        if camera_info is None:
            self.get_logger().warn('Could not get camera info - cannot project gimbal fov.')
            return
        assert hasattr(camera_info, 'k'), 'Camera intrinsics matrix K not available - cannot project gimbal FoV.'
        h, w = self._img_dimensions()
        # TODO: assert h w not none and integers? and divisible by 2?

        # Intrinsic matrix
        k = np.array(camera_info.k).reshape([3, 3])
        assert k.shape == (3, 3), 'Intrinsic matrix had unexpected shape: ' + str(k.shape) \
                                  + ' - could not project gimbal FoV.'

        # Project image corners to z=0 plane (ground)
        src_corners = create_src_corners(h, w)

        e = np.delete(e, 2, 1)  # Remove z-column, making the matrix square
        p = np.matmul(k, e)
        p_inv = np.linalg.inv(p)

        dst_corners = cv2.perspectiveTransform(src_corners, p_inv)  # TODO: use util.get_fov here?
        dst_corners = dst_corners.squeeze()  # See get_fov usage elsewhere -where to do squeeze if at all?

        return dst_corners

    def _get_global_position_latlonalt(self):
        """Returns lat, lon in WGS84 and altitude in meters from vehicle global position."""
        global_position = self._global_position()
        if global_position is None:
            self.get_logger().warn('Could not get vehicle global position.')
            return None
        assert hasattr(global_position, 'lat') and hasattr(global_position, 'lon'),\
            'Global position message did not include lat or lon fields.'
        assert hasattr(global_position, 'alt'), 'Global position message did not include alt field.'
        lat, lon, alt = global_position.lat, global_position.lon, global_position.alt
        return LatLonAlt(lat, lon, alt)

    def _update_map(self):
        """Gets latest map from WMS server and saves it."""
        global_position_latlonalt = self._get_global_position_latlonalt()
        if global_position_latlonalt is None:
            self.get_logger().warn('Could not get vehicle global position latlonalt. Cannot update map.')
            return None

        # Use these coordinates for fetching map from server
        map_center_latlon = LatLon(global_position_latlonalt.lat, global_position_latlonalt.lon)

        if self._use_gimbal_projection():
            camera_info = self._camera_info()
            if camera_info is not None:
                assert hasattr(camera_info, 'k'), 'CameraInfo does not have k, cannot compute gimbal FoV WGS84 ' \
                                                  'coordinates. '

                gimbal_fov_pix = self._project_gimbal_fov(global_position_latlonalt.alt)

                # Convert gimbal field of view from pixels to WGS84 coordinates
                if gimbal_fov_pix is not None:
                    azimuths = list(map(lambda x: math.degrees(math.atan2(x[0], x[1])), gimbal_fov_pix))
                    distances = list(map(lambda x: math.sqrt(x[0]**2 + x[1]**2), gimbal_fov_pix))
                    zipped = list(zip(azimuths, distances))
                    to_wgs84 = partial(move_distance, map_center_latlon)
                    self._gimbal_fov_wgs84 = np.array(list(map(to_wgs84, zipped)))
                    ### TODO: add some sort of assertion hat projected FoV is contained in size and makes sense

                    # Use projected field of view center instead of global position as map center
                    map_center_latlon = get_bbox_center(fov_to_bbox(self._gimbal_fov_wgs84))
                else:
                    self.get_logger().warn('Could not project camera FoV, getting map raster assuming nadir-facing '
                                           'camera.')
            else:
                self.get_logger().debug('Camera info not available, cannot project gimbal FoV, defaulting to global '
                                        'position.')

        self._map_bbox = get_bbox(map_center_latlon)

        # Build and send WMS request
        layer_str = self.get_parameter('layer').get_parameter_value().string_value
        srs_str = self.get_parameter('srs').get_parameter_value().string_value
        self.get_logger().debug(f'Getting map for bounding box: {self._map_bbox}, layer: {layer_str}, srs: {srs_str}.')
        try:
            self._map = self._wms.getmap(layers=[layer_str], srs=srs_str, bbox=self._map_bbox,
                                         size=self._map_size_with_padding(), format='image/png',
                                         transparent=True)
        except Exception as e:
            self.get_logger().warn('Exception from WMS server query: {}\n{}'.format(e, traceback.print_exc()))
            return

        # Decode response from WMS server
        self._map = np.frombuffer(self._map.read(), np.uint8)
        self._map = imdecode(self._map, cv2.IMREAD_UNCHANGED)
        assert self._map.shape[0:2] == self._map_size_with_padding(), 'Decoded map is not the specified size.'

    def _image_raw_callback(self, msg):
        """Handles reception of latest image frame from camera."""
        self.get_logger().debug('Camera image callback triggered.')
        self._topics_msgs['image_raw'] = msg
        self._cv_image = self._cv_bridge.imgmsg_to_cv2(msg, 'bgr8')
        img_size = self._declared_img_size()
        if img_size is not None:
            assert self._cv_image.shape[0:2] == self._declared_img_size(), 'Converted _cv_image shape {} did not match ' \
                                                                           'declared image shape {}.'.format(
                self._cv_image.shape[0:2], self._declared_img_size())
        self._match()

    def _camera_yaw(self):
        """Returns camera yaw in degrees."""
        rpy = self._get_camera_rpy()

        assert rpy is not None, 'RPY is None, cannot retrieve camera yaw.'
        assert len(rpy) == 3, f'Unexpected length for RPY: {len(rpy)}.'
        assert hasattr(rpy, 'yaw'), f'No yaw attribute found for named tuple: {rpy}.'

        camera_yaw = rpy.yaw
        return camera_yaw

    def _get_camera_rpy(self):
        """Returns roll-pitch-yaw euler vector."""
        gimbal_attitude = self._gimbal_attitude()
        if gimbal_attitude is None:
            self.get_logger().warn('Gimbal attitude not available, cannot return RPY.')
            return None
        assert hasattr(gimbal_attitude, 'q'), 'Gimbal attitude quaternion not available - cannot compute RPY.'
        gimbal_euler = Rotation.from_quat(gimbal_attitude.q).as_euler(self.EULER_SEQUENCE, degrees=True)

        local_position = self._topics_msgs.get('VehicleLocalPosition', None)
        if local_position is None:
            self.get_logger().warn('VehicleLocalPosition is unknown, cannot get heading. Cannot return RPY.')
            return None
        assert hasattr(local_position, 'heading'), 'Heading information missing from VehicleLocalPosition message. ' \
                                                   'Cannot compute RPY. '

        pitch_index = self._pitch_index()
        assert pitch_index != -1, 'Could not identify pitch index in gimbal attitude, cannot return RPY.'

        yaw_index = self._yaw_index()
        assert yaw_index != -1, 'Could not identify yaw index in gimbal attitude, cannot return RPY.'

        self.get_logger().warn('Assuming stabilized gimbal - ignoring vehicle intrinsic pitch and roll for camera RPY.')
        self.get_logger().warn('Assuming zero roll for camera RPY.')

        heading = local_position.heading
        assert -math.pi <= heading <= math.pi, 'Unexpected heading value: ' + str(
            heading) + '([-pi, pi] expected). Cannot compute RPY.'
        heading = math.degrees(heading)

        gimbal_yaw = gimbal_euler[yaw_index]
        assert -180 <= gimbal_yaw <= 180, 'Unexpected gimbal yaw value: ' + str(
            heading) + '([-180, 180] expected). Cannot compute RPY. '
        yaw = heading + gimbal_yaw
        pitch = -(90 + gimbal_euler[pitch_index])
        roll = 0
        rpy = RPY(roll, pitch, yaw)

        return rpy

    def _get_camera_normal(self):
        nadir = np.array([0, 0, 1])
        rpy = self._get_camera_rpy()
        if rpy is None:
            self.get_logger().warn('Could not get RPY - cannot compute camera normal.')
            return None

        r = Rotation.from_euler(self.EULER_SEQUENCE, list(rpy), degrees=True)
        camera_normal = r.apply(nadir)

        assert camera_normal.shape == nadir.shape, f'Unexpected camera normal shape {camera_normal.shape}.'
        # TODO: this assertion is arbitrary? how to handle unexpected camera normal length?
        camera_normal_length = np.linalg.norm(camera_normal)
        assert abs(camera_normal_length-1) <= 0.001, f'Unexpected camera normal length {camera_normal_length}.'

        return camera_normal

    def _pitch_index(self):
        return self.EULER_SEQUENCE.lower().find('y')

    def _yaw_index(self):
        return self.EULER_SEQUENCE.lower().find('x')

    def _base_callback(self, msg_name, msg):
        """Stores message and prints out brief debug log message."""
        self.get_logger().debug(msg_name + ' callback triggered.')
        self._topics_msgs[msg_name] = msg

    def _camera_info_callback(self, msg):
        """Handles reception of camera info."""
        self._base_callback('camera_info', msg)
        self.get_logger().debug('Camera info: ' + str(msg))

        # Check that key fields are present in received msg, then destroy subscription which is no longer needed
        if msg is not None and hasattr(msg, 'K') and hasattr(msg, 'width') and hasattr(msg, 'height'):
            self._topics['camera_info'].destroy()

    def _vehiclelocalposition_pubsubtopic_callback(self, msg):
        """Handles reception of latest local position estimate."""
        self._base_callback('VehicleLocalPosition', msg)

    def _vehicleglobalposition_pubsubtopic_callback(self, msg):
        """Handles reception of latest global position estimate."""
        self._base_callback('VehicleGlobalPosition', msg)
        self._update_map()

    def _gimbaldeviceattitudestatus_pubsubtopic_callback(self, msg):
        """Handles reception of GimbalDeviceAttitudeStatus messages."""
        self._base_callback('GimbalDeviceAttitudeStatus', msg)

    def _gimbaldevicesetattitude_pubsubtopic_callback(self, msg):
        """Handles reception of GimbalDeviceSetAttitude messages."""
        self._base_callback('GimbalDeviceSetAttitude', msg)

    def _vehicleattitude_pubsubtopic_callback(self, msg):
        """Handles reception of VehicleAttitude messages."""
        self._base_callback('VehicleAttitude', msg)

    def _camera_pitch(self):
        """Returns camera pitch in degrees relative to vehicle frame."""
        rpy = self._get_camera_rpy()
        if rpy is None:
            self.get_logger().warn('Gimbal RPY not available, cannot compute camera pitch.')

        assert len(rpy) == 3, 'Unexpected length of euler angles vector: ' + str(len(rpy))
        assert hasattr(rpy, 'pitch'), f'Pitch attribute not found in named tuple {rpy}.'

        return rpy.pitch

    def _gimbal_attitude(self):
        """Returns GimbalDeviceAttitudeStatus or GimbalDeviceSetAttitude if it is not available."""
        gimbal_attitude = self._topics_msgs.get('GimbalDeviceAttitudeStatus', None)
        if gimbal_attitude is None:
            # Try alternative topic
            self.get_logger().warn('GimbalDeviceAttitudeStatus not available. Trying GimbalDeviceSetAttitude instead.')
            gimbal_attitude = self._topics_msgs.get('GimbalDeviceSetAttitude', None)
            if gimbal_attitude is None:
                self.get_logger().warn('GimbalDeviceSetAttitude not available. Gimbal attitude status not available.')
        return gimbal_attitude

    def _process_matches(self, mkp_img, mkp_map, k, camera_normal, reproj_threshold=1.0, method=cv2.RANSAC, affine=False):
        """Processes matching keypoints from img and map and returns essential, and homography matrices & pose.

        Arguments:
            mkp_img - The matching keypoints from image.
            mkp_map - The matching keypoints from map.
            k - The intrinsic camera matrix.
            camera_normal - The camera normal unit vector.
            reproj_threshold - The RANSAC reprojection threshold for homography estimation.
            method - Method to use for estimation.
            affine - Boolean flag indicating that transformation should be restricted to 2D affine transformation
        """
        min_points = 4
        assert len(mkp_img) >= min_points and len(mkp_map) >= min_points, 'Four points needed to estimate homography.'
        if not affine:
            h, h_mask = cv2.findHomography(mkp_img, mkp_map, method, reproj_threshold)
        else:
            h, h_mask = cv2.estimateAffinePartial2D(mkp_img, mkp_map)
            h = np.vstack((h, np.array([0, 0, 1])))  # Make it into a homography matrix

        num, Rs, Ts, Ns = cv2.decomposeHomographyMat(h, k)

        # Get the one where angle between plane normal and inverse of camera normal is smallest
        # Plane is defined by Z=0 and "up" is in the negative direction on the z-axis in this case
        get_angle_partial = partial(get_angle, -camera_normal)
        angles = list(map(get_angle_partial, Ns))
        index_of_smallest_angle = angles.index(min(angles))
        rotation, translation = Rs[index_of_smallest_angle], Ts[index_of_smallest_angle]

        self.get_logger().debug('decomposition R:\n{}.'.format(rotation))
        self.get_logger().debug('decomposition T:\n{}.'.format(translation))
        self.get_logger().debug('decomposition Ns:\n{}.'.format(Ns))
        self.get_logger().debug('decomposition Ns angles:\n{}.'.format(angles))
        self.get_logger().debug('decomposition smallest angle index:\n{}.'.format(index_of_smallest_angle))
        self.get_logger().debug('decomposition smallest angle:\n{}.'.format(min(angles)))

        return h, h_mask, translation, rotation

    def _match(self):
        """Matches camera image to map image and computes camera position and field of view."""
        try:
            self.get_logger().debug('Matching image to map.')

            if self._map is None:
                self.get_logger().warn('Map not yet available - skipping matching.')
                return

            yaw = self._camera_yaw()
            if yaw is None:
                self.get_logger().warn('Could not get camera yaw. Skipping matching.')
                return
            rot = math.radians(yaw)
            assert -math.pi <= rot <= math.pi, 'Unexpected gimbal yaw value: ' + str(rot) + ' ([-pi, pi] expected).'
            self.get_logger().debug('Current camera yaw: ' + str(rot) + ' radians.')

            map_cropped = rotate_and_crop_map(self._map, rot, self._img_dimensions())
            assert map_cropped.shape[0:2] == self._declared_img_size(), 'Cropped map did not match declared shape.'

            mkp_img, mkp_map = self._superglue.match(self._cv_image, map_cropped)

            match_count_img = len(mkp_img)
            assert match_count_img == len(mkp_map), 'Matched keypoint counts did not match.'
            if match_count_img < self.MINIMUM_MATCHES:
                self.get_logger().warn(f'Found {match_count_img} matches, {self.MINIMUM_MATCHES} required. Skip frame.')
                return

            camera_normal = self._get_camera_normal()
            if camera_normal is None:
                self.get_logger().warn('Could not get camera normal. Skipping matching.')
                return

            camera_info = self._camera_info()
            if camera_info is None:
                self.get_logger().warn('Could not get camera info. Skipping matching.')
                return
            assert hasattr(camera_info, 'k'), 'Camera info did not have k - cannot match.'
            assert len(camera_info.k) == 9, 'K had unexpected length.'
            k = camera_info.k.reshape([3, 3])

            h, h_mask, t, r = self._process_matches(mkp_img, mkp_map, k, camera_normal, affine=self._restrict_affine())

            assert h.shape == (3, 3), f'Homography matrix had unexpected shape: {h.shape}.'
            assert t.shape == (3, 1), f'Translation vector had unexpected shape: {t.shape}.'
            assert r.shape == (3, 3), f'Rotation matrix had unexpected shape: {r.shape}.'

            fov_pix = get_fov(self._cv_image, h)
            visualize_homography('Matches and FoV', self._cv_image, map_cropped, mkp_img, mkp_map, fov_pix)

            map_lat, map_lon = get_bbox_center(BBox(*self._map_bbox))

            map_dims_with_padding = self._map_dimensions_with_padding()
            if map_dims_with_padding is None:
                self.get_logger().warn('Could not get map dimensions info. Skipping matching.')
                return

            img_dimensions = self._img_dimensions()
            if map_dims_with_padding is None:
                self.get_logger().warn('Could not get img dimensions info. Skipping matching.')
                return

            fov_wgs84, fov_uncropped, fov_unrotated = convert_fov_from_pix_to_wgs84(
                fov_pix, map_dims_with_padding, self._map_bbox, rot, img_dimensions)

            # Compute camera altitude, and distance to principal point using triangle similarity
            # TODO: _update_map has similar logic used in gimbal fov projection, try to combine
            fov_center_line_length = get_distance_of_fov_center(fov_wgs84)
            focal_length = k[0]
            assert hasattr(img_dimensions, 'width') and hasattr(img_dimensions, 'height'), \
                'Img dimensions did not have expected attributes.'
            camera_distance = get_camera_distance(focal_length, img_dimensions.width, fov_center_line_length)
            camera_pitch = self._camera_pitch()
            camera_altitude = None
            if camera_pitch is None:
                # TODO: Use some other method to estimate altitude if pitch not available?
                self.get_logger().warn('Camera pitch not available - cannot estimate altitude visually.')
            else:
                camera_altitude = math.cos(math.radians(camera_pitch)) * camera_distance  # TODO: use rotation from decomposeHomography for getting the pitch in this case (use visual info, not from sensors)
            self.get_logger().debug(f'Camera pitch {camera_pitch} deg, distance to principal point {camera_distance} m,'
                                    f' altitude {camera_altitude} m.')

            #### TODO: remove this debugging section
            """
            mkp_map_uncropped = []
            for i in range(0, len(mkp_map)):
                mkp_map_uncropped.append(list(
                    uncrop_pixel_coordinates(self._img_dimensions(), self._map_dimensions_with_padding(), mkp_map[i])))
            mkp_map_uncropped = np.array(mkp_map_uncropped)

            mkp_map_unrotated = []
            for i in range(0, len(mkp_map_uncropped)):
                mkp_map_unrotated.append(
                    list(rotate_point(rot, self._map_dimensions_with_padding(), mkp_map_uncropped[i])))
            mkp_map_unrotated = np.array(mkp_map_unrotated)

            h2, h_mask2, translation_vector2, rotation_matrix2 = self._process_matches(mkp_img, mkp_map_unrotated,
                                                                                 # mkp_map_uncropped,
                                                                                 self._camera_info().k.reshape([3, 3]),
                                                                                 cam_normal,
                                                                                 affine=
                                                                                 self._config['misc']['affine'])

            fov_pix_2 = get_fov(self._cv_image, h2)
            visualize_homography('Uncropped and unrotated', self._cv_image, self._map, mkp_img, mkp_map_unrotated,
                                 fov_pix_2)  # TODO: separate calculation of fov_pix from their visualization!
            """
            #### END DEBUG SECTION ###

            # Convert translation vector to WGS84 coordinates
            # Translate relative to top left corner, not principal point/center of map raster
            h, w = img_dimensions
            t[0] = (1 - t[0]) * w / 2
            t[1] = (1 - t[1]) * h / 2
            cam_pos_wgs84, cam_pos_wgs84_uncropped, cam_pos_wgs84_unrotated = convert_fov_from_pix_to_wgs84(  # TODO: break this func into an array and single version?
                np.array(t[0:2].reshape((1, 1, 2))), map_dims_with_padding,
                self._map_bbox, rot, img_dimensions)

            cam_pos_wgs84 = cam_pos_wgs84.squeeze()  # TODO: eliminate need for this squeeze

            fov_gimbal = self._gimbal_fov_wgs84
            write_fov_and_camera_location_to_geojson(fov_wgs84, cam_pos_wgs84, (map_lat, map_lon, camera_distance),
                                                     fov_gimbal)
            # self._essential_mat_pub.publish(e)
            # self._homography_mat_pub.publish(h)
            # self._pose_pub.publish(p)
            self._topics['fov'].publish(fov_wgs84)

        except Exception as e:
            self.get_logger().error('Matching returned exception: {}\n{}'.format(e, traceback.print_exc()))


def main(args=None):
    rclpy.init(args=args)
    matcher = Matcher(share_dir, superglue_dir)
    rclpy.spin(matcher)
    matcher.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()

"""ROS 2 package for estimating airborne drone global position by matching
video to map retrieved from onboard GIS server

All ROS 2 nodes are defined in dedicated modules to keep individual file size
down. They are imported here to package namespace for convenience. For example:

.. code-block::

    #from gisnav.core.gis_node import GISNode
    from gisnav import GISNode

ROS namespace and :term:`core` node names are hard-coded inside the static
public node entrypoints defined here. Other node initialization arguments are p
rovided via ROS 2 launch arguments.
"""
import cProfile
import io
import pstats
from typing import Optional

import rclpy
from rclpy.node import Node

from .constants import (
    BBOX_NODE_NAME,
    GIS_NODE_NAME,
    NMEA_NODE_NAME,
    POSE_NODE_NAME,
    QGIS_NODE_NAME,
    ROS_NAMESPACE,
    STEREO_NODE_NAME,
    UORB_NODE_NAME,
)
from .core import BBoxNode, GISNode, PoseNode, StereoNode

try:
    from .extensions.qgis_node import QGISNode

    def run_qgis_node():
        """Spins up a :class:`.QGISNode`"""
        _run(QGISNode, QGIS_NODE_NAME, **_rclpy_node_kwargs)

except ModuleNotFoundError as e:
    print(f"Could not import QGISNode because a module was not found: {e}")

try:
    from .extensions.nmea_node import NMEANode

    def run_nmea_node():
        """Spins up a :class:`.NMEANode`"""
        _run(NMEANode, NMEA_NODE_NAME, **_rclpy_node_kwargs)

except ModuleNotFoundError as e:
    print(f"Could not import NMEANode because a module was not found: {e}")

try:
    from .extensions.uorb_node import UORBNode

    def run_uorb_node():
        """Spins up a :class:`.UORBNode`"""
        _run(UORBNode, UORB_NODE_NAME, **_rclpy_node_kwargs)

except ModuleNotFoundError as e:
    print(f"Could not import NMEANode because a module was not found: {e}")


def _run(constructor: rclpy.node.Node, *args, **kwargs):
    """Spins up a ROS 2 node

    :param constructor: Node constructor
    :param *args: Node constructor args
    :param **kwargs: Node constructor kwargs
    :return:
    """
    if __debug__:
        profile = cProfile.Profile()
        profile.enable()
    else:
        profile = None

    node: Optional[Node] = None
    try:
        rclpy.init()
        node = constructor(*args, **kwargs)
        rclpy.spin(node)
    except KeyboardInterrupt as e:
        print(f"Keyboard interrupt received:\n{e}")
        if profile is not None:
            assert __debug__
            # Print out cProfile stats
            profile.disable()
            s = io.StringIO()
            stats = pstats.Stats(profile, stream=s).sort_stats(pstats.SortKey.TIME)
            stats.print_stats(20)
            if node is not None:
                node.get_logger().info(s.getvalue())
    finally:
        if node is not None:
            node.destroy_node()
        rclpy.shutdown()


_rclpy_node_kwargs = {
    "namespace": ROS_NAMESPACE,
    "allow_undeclared_parameters": True,
    "automatically_declare_parameters_from_overrides": True,
}


def run_bbox_node():
    """Spins up a :class:`.BBoxNode`"""
    _run(BBoxNode, BBOX_NODE_NAME, **_rclpy_node_kwargs)


def run_gis_node():
    """Spins up a :class:`.GISNode`"""
    _run(GISNode, GIS_NODE_NAME, **_rclpy_node_kwargs)


def run_stereo_node():
    """Spins up a :class:`.StereoNode`"""
    _run(StereoNode, STEREO_NODE_NAME, **_rclpy_node_kwargs)


def run_pose_node():
    """Spins up a :class:`.PoseNode`"""
    _run(PoseNode, POSE_NODE_NAME, **_rclpy_node_kwargs)

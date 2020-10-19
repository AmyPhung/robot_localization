#!/usr/bin/env python

""" This is the starter code for the robot localization project """

import rospy

from std_msgs.msg import Header, String
from sensor_msgs.msg import LaserScan, PointCloud
from geometry_msgs.msg import PoseStamped, PoseWithCovarianceStamped, PoseArray, Pose, Point, Quaternion
from robot_localizer.msg import Particle, ParticleArray
from nav_msgs.srv import GetMap
from copy import deepcopy

import tf
from tf import TransformListener
from tf import TransformBroadcaster
from tf.transformations import euler_from_quaternion, rotation_matrix, quaternion_from_matrix
from random import gauss

import math
import time

import numpy as np
from numpy.random import random_sample
from sklearn.neighbors import NearestNeighbors
from occupancy_field import OccupancyField
from helper_functions import TFHelper

class Particle(object):
    """ Represents a hypothesis (particle) of the robot's pose consisting of x,y and theta (yaw)
        Attributes:
            x: the x-coordinate of the hypothesis relative to the map frame
            y: the y-coordinate of the hypothesis relative ot the map frame
            theta: the yaw of the hypothesis relative to the map frame
            w: the particle weight (the class does not ensure that particle weights are normalized
    """

    def __init__(self,x=0.0,y=0.0,theta=0.0,w=1.0):
        """ Construct a new Particle
            x: the x-coordinate of the hypothesis relative to the map frame
            y: the y-coordinate of the hypothesis relative ot the map frame
            theta: the yaw of the hypothesis relative to the map frame
            w: the particle weight (the class does not ensure that particle weights are normalized """
        self.w = w
        self.theta = theta
        self.x = x
        self.y = y

    def as_pose(self):
        """ A helper function to convert a particle to a geometry_msgs/Pose message """
        orientation_tuple = tf.transformations.quaternion_from_euler(0,0,self.theta)
        return Pose(position=Point(x=self.x,y=self.y,z=0), orientation=Quaternion(x=orientation_tuple[0], y=orientation_tuple[1], z=orientation_tuple[2], w=orientation_tuple[3]))

    # TODO: define additional helper functions if needed

class ParticleFilter:
    """ The class that represents a Particle Filter ROS Node
        Attributes list:
            initialized: a Boolean flag to communicate to other class methods that initializaiton is complete
            base_frame: the name of the robot base coordinate frame (should be "base_link" for most robots)
            map_frame: the name of the map coordinate frame (should be "map" in most cases)
            odom_frame: the name of the odometry coordinate frame (should be "odom" in most cases)
            scan_topic: the name of the scan topic to listen to (should be "scan" in most cases)
            n_particles: the number of particles in the filter
            d_thresh: the amount of linear movement before triggering a filter update
            a_thresh: the amount of angular movement before triggering a filter update
            laser_max_distance: the maximum distance to an obstacle we should use in a likelihood calculation
            pose_listener: a subscriber that listens for new approximate pose estimates (i.e. generated through the rviz GUI)
            particle_pub: a publisher for the particle cloud
            laser_subscriber: listens for new scan data on topic self.scan_topic
            tf_listener: listener for coordinate transforms
            tf_broadcaster: broadcaster for coordinate transforms
            particle_cloud: a list of particles representing a probability distribution over robot poses
            current_odom_xy_theta: the pose of the robot in the odometry frame when the last filter update was performed.
                                   The pose is expressed as a list [x,y,theta] (where theta is the yaw)
            map: the map we will be localizing ourselves in.  The map should be of type nav_msgs/OccupancyGrid
    """
    def __init__(self):
        self.initialized = False        # make sure we don't perform updates before everything is setup
        rospy.init_node('pf')           # tell roscore that we are creating a new node named "pf"

        self.base_frame = "base_link"   # the frame of the robot base
        self.map_frame = "map"          # the name of the map coordinate frame
        self.odom_frame = "odom"        # the name of the odometry coordinate frame
        self.scan_topic = "scan"        # the topic where we will get laser scans from

        self.n_particles = 300                # the number of particles to use
        self.initial_uncertainty_xy = 1       # Amplitute factor of initial x and y uncertainty
        self.initial_uncertainty_theta = 0.5  # Amplitude factor of initial theta uncertainty
        self.variance_scale = 0.15             # Scaling term for variance effect on resampling
        # self.resample_noise_xy = 0.1          # Amplitude factor of resample x and y noise
        # self.resample_noise_theta = 0.1       # Amplitude factor of resample theta noise

        self.d_thresh = 0.2             # the amount of linear movement before performing an update
        self.a_thresh = math.pi/6       # the amount of angular movement before performing an update

        self.laser_max_distance = 2.0   # maximum penalty to assess in the likelihood field model

        # TODO: define additional constants if needed

        # Setup pubs and subs

        # pose_listener responds to selection of a new approximate robot location (for instance using rviz)
        rospy.Subscriber("initialpose", PoseWithCovarianceStamped, self.update_initial_pose)

        # publish the current particle cloud.  This enables viewing particles in rviz.
        self.particle_pub = rospy.Publisher("particlecloud", PoseArray, queue_size=10)
        # publish custom particle array messge type
        self.particle_viz_pub = rospy.Publisher("weighted_particlecloud", ParticleArray, queue_size=10)

        # laser_subscriber listens for data from the lidar
        rospy.Subscriber(self.scan_topic, LaserScan, self.scan_received)

        # enable listening for and broadcasting coordinate transforms
        self.tf_listener = TransformListener()
        self.tf_broadcaster = TransformBroadcaster()

        self.particle_cloud = []

        # change use_projected_stable_scan to True to use point clouds instead of laser scans
        self.use_projected_stable_scan = False
        self.last_projected_stable_scan = None
        if self.use_projected_stable_scan:
            # subscriber to the odom point cloud
            rospy.Subscriber("projected_stable_scan", PointCloud, self.projected_scan_received)

        self.current_odom_xy_theta = []
        self.occupancy_field = OccupancyField()
        self.transform_helper = TFHelper()
        self.initialized = True

    def update_robot_pose(self, timestamp):
        """ Update the estimate of the robot's pose given the updated particles.
            There are two logical methods for this:
                (1): compute the mean pose
                (2): compute the most likely pose (i.e. the mode of the distribution)
        """
        # first make sure that the particle weights are normalized
        self.normalize_particles()

        # sum_x, sum_y, sum_theta = 0,0,0

        # TODO: get 20 best particles

        max_weight = 0
        best_particle = None
        for p in self.particle_cloud:
            if p.w > max_weight:
                max_weight = p.w
                best_particle = p

            # sum_x += p.x
            # sum_y += p.y
            # sum_theta += p.theta

        # Assign the latest pose into self.robot_pose as a Pose object
        self.robot_pose = self.transform_helper.convert_xy_and_theta_to_pose(
                best_particle.x,
                best_particle.y,
                best_particle.theta)

        self.transform_helper.fix_map_to_odom_transform(self.robot_pose, timestamp)

    def projected_scan_received(self, msg):
        self.last_projected_stable_scan = msg

    def update_particles_with_odom(self, msg):
        """ Update the particles using the newly given odometry pose.
            The function computes the value delta which is a tuple (x,y,theta)
            that indicates the change in position and angle between the odometry
            when the particles were last updated and the current odometry.

            msg: this is not really needed to implement this, but is here just in case.
        """
        new_odom_xy_theta = self.transform_helper.convert_pose_to_xy_and_theta(self.odom_pose.pose)
        # compute the change in x,y,theta since our last update
        if self.current_odom_xy_theta:
            old_odom_xy_theta = self.current_odom_xy_theta
            delta = (new_odom_xy_theta[0] - self.current_odom_xy_theta[0],
                     new_odom_xy_theta[1] - self.current_odom_xy_theta[1],
                     new_odom_xy_theta[2] - self.current_odom_xy_theta[2])# COUNTERCLOCKWISE

            self.current_odom_xy_theta = new_odom_xy_theta
        else:
            self.current_odom_xy_theta = new_odom_xy_theta
            return

        # update particles
        r = math.sqrt(delta[0]**2 + delta[1]**2)

        for particle in self.particle_cloud:
        	particle.x += r*math.cos(particle.theta)
        	particle.y += r*math.sin(particle.theta)
        	particle.theta += delta[2]

    def map_calc_range(self,x,y,theta):
        """ Difficulty Level 3: implement a ray tracing likelihood model... Let me know if you are interested """
        # TODO: nothing unless you want to try this alternate likelihood model
        pass

    def resample_particles(self):
        """ Resample the particles according to the new particle weights.
            The weights stored with each particle should define the probability that a particular
            particle is selected in the resampling step.  You may want to make use of the given helper
            function draw_random_sample.
        """
        xs, ys, thetas, weights = [],[],[],[]

        # Make a list of particle stats for draw_random_sample to use
        for p in self.particle_cloud:
            xs.append(p.x)
            ys.append(p.y)
            thetas.append(p.theta)
            weights.append(p.w)

        # Throw out some particles
        self.particle_cloud = self.draw_random_sample(self.particle_cloud,
            weights, self.n_particles)

        # Compute variance of particles
        x_var = np.var(xs)
        y_var = np.var(ys)
        theta_var = np.var(weights)

        # Cap our level of confidence in the particles
        if x_var < 0.1: x_var = 0.1
        if y_var < 0.1: y_var = 0.1
        if theta_var < 10: theta_var = 10

        # Inject some noise into the new cloud based on current variance
        for p in self.particle_cloud:
            noise = np.random.randn(3)
            p.x #+= noise[0] * x_var * self.variance_scale
            p.y #+= noise[1] * y_var * self.variance_scale
            p.theta #+= noise[2] * theta_var * self.variance_scale

        self.normalize_particles()

    def update_particles_with_laser(self, msg):
        """ Updates the particle weights in response to the scan contained in the msg """
        for p in self.particle_cloud:
            # Compute delta to x,y coords in map frame of each lidar point assuming
            # lidar is centered at the base_link
            # TODO: Account for the offset between the lidar and the base_link
            angles = np.arange(0, 361, dtype=float)
            dxs = msg.ranges * np.cos(np.degrees(angles + p.theta))
            dys = msg.ranges * np.sin(np.degrees(angles + p.theta))

            # Initialize total distance to 0
            d = 0
            # Initialize number of valid points
            valid_pts = len(dxs)

            for dx,dy in zip(dxs, dys):
                # Ignore points with invalid ranges
                if dx == 0 and dy == 0:
                    continue

                # Apply delta
                x = p.x + dx
                y = p.y + dy

                # Find nearest point to each lidar point according to map
                dist = self.occupancy_field.get_closest_obstacle_distance(x, y)
                # Check to make sure lidar point is actually on the map
                if not np.isnan(dist):
                    d += dist
                else:
                    valid_pts -= 1

            # If there aren't enough valid points for the particle, assume that it's
            # not good
            # TODO: Add a ROS param threshold for this
            if valid_pts < 10:
                p.w = 0
            else:
                # Update particle weight based on inverse of average squared difference
                if d != 0:
                    p.w = 1 / ((d ** 2)/valid_pts)
                else:
                    # If difference is exactly 0, something's likely wrong
                    rospy.logwarn("Computed difference between particle projection and lidar scan is exactly 0")

        self.normalize_particles()

    @staticmethod
    def draw_random_sample(choices, probabilities, n):
        """ Return a random sample of n elements from the set choices with the specified probabilities
            choices: the values to sample from represented as a list
            probabilities: the probability of selecting each element in choices represented as a list
            n: the number of samples
        """
        values = np.array(range(len(choices)))
        probs = np.array(probabilities)
        bins = np.add.accumulate(probs)
        inds = values[np.digitize(random_sample(n), bins)]
        samples = []
        for i in inds:
            samples.append(deepcopy(choices[int(i)]))
        return samples

    def update_initial_pose(self, msg):
        """ Callback function to handle re-initializing the particle filter based on a pose estimate.
            These pose estimates could be generated by another ROS Node or could come from the rviz GUI """
        xy_theta = self.transform_helper.convert_pose_to_xy_and_theta(msg.pose.pose)
        self.initialize_particle_cloud(msg.header.stamp, xy_theta)

    def initialize_particle_cloud(self, timestamp, xy_theta=None):
        """ Initialize the particle cloud.
            Arguments
            xy_theta: a triple consisting of the mean x, y, and theta (yaw) to initialize the
                      particle cloud around.  If this input is omitted, the odometry will be used """
        if xy_theta is None:
            xy_theta = self.transform_helper.convert_pose_to_xy_and_theta(self.odom_pose.pose)
        self.particle_cloud = []

        for i in range(self.n_particles):
            noise = (np.random.randn(3)*self.initial_uncertainty_xy)
            noise[2] = np.random.randn()*self.initial_uncertainty_theta
            new_pose = np.array(xy_theta) + noise
            new_particle = Particle(*new_pose)
            self.particle_cloud.append(new_particle)

        self.normalize_particles()

    def normalize_particles(self):
        """ Make sure the particle weights define a valid distribution (i.e. sum to 1.0) """
        cumulative_weight = 0

        # Compute cumulative weight
        for p in self.particle_cloud:
            cumulative_weight += p.w

        # Normalize weights
        for p in self.particle_cloud:
            p.w = p.w/cumulative_weight

    def publish_particles(self, msg):
        particles_conv = []
        custom_particle_msgs = []

        for p in self.particle_cloud:
            particle_pose = p.as_pose()
            particles_conv.append(particle_pose)

            # Create new particle message
            new_particle = Particle()
            new_particle.pose = particle_pose
            new_particle.weight = p.w
            custom_particle_msgs.append(new_particle)
        # actually send the message so that we can view it in rviz
        self.particle_pub.publish(PoseArray(header=Header(stamp=rospy.Time.now(),
                                            frame_id=self.map_frame),
                                  poses=particles_conv))
        # send our custom message to visualize weights in rviz
        self.particle_viz_pub.publish(ParticleArray(header=Header(stamp=rospy.Time.now(),
                                                    frame_id=self.map_frame),
                                      particles=custom_particle_msgs))

    def scan_received(self, msg):
        """ This is the default logic for what to do when processing scan data.
            Feel free to modify this, however, we hope it will provide a good
            guide.  The input msg is an object of type sensor_msgs/LaserScan """
        if not(self.initialized):
            # wait for initialization to complete
            rospy.loginfo_once("Waiting for initial pose estimate...")
            return
        else:
            rospy.loginfo_once("Initial pose estimate found!")

        # wait a little while to see if the transform becomes available.  This fixes a race
        # condition where the scan would arrive a little bit before the odom to base_link transform
        # was updated.
        self.tf_listener.waitForTransform(self.base_frame, msg.header.frame_id, msg.header.stamp, rospy.Duration(0.5))
        if not(self.tf_listener.canTransform(self.base_frame, msg.header.frame_id, msg.header.stamp)):
            # need to know how to transform the laser to the base frame
            # this will be given by either Gazebo or neato_node
            return

        if not(self.tf_listener.canTransform(self.base_frame, self.odom_frame, msg.header.stamp)):
            # need to know how to transform between base and odometric frames
            # this will eventually be published by either Gazebo or neato_node
            return

        # calculate pose of laser relative to the robot base
        p = PoseStamped(header=Header(stamp=rospy.Time(0),
                                      frame_id=msg.header.frame_id))
        self.laser_pose = self.tf_listener.transformPose(self.base_frame, p)

        # find out where the robot thinks it is based on its odometry
        p = PoseStamped(header=Header(stamp=msg.header.stamp,
                                      frame_id=self.base_frame),
                        pose=Pose())
        self.odom_pose = self.tf_listener.transformPose(self.odom_frame, p)
        # store the the odometry pose in a more convenient format (x,y,theta)
        new_odom_xy_theta = self.transform_helper.convert_pose_to_xy_and_theta(self.odom_pose.pose)
        if not self.current_odom_xy_theta:
            self.current_odom_xy_theta = new_odom_xy_theta
            return

        if not(self.particle_cloud):
            # now that we have all of the necessary transforms we can update the particle cloud
            self.initialize_particle_cloud(msg.header.stamp)
        elif (math.fabs(new_odom_xy_theta[0] - self.current_odom_xy_theta[0]) > self.d_thresh or
              math.fabs(new_odom_xy_theta[1] - self.current_odom_xy_theta[1]) > self.d_thresh or
              math.fabs(new_odom_xy_theta[2] - self.current_odom_xy_theta[2]) > self.a_thresh):
            # we have moved far enough to do an update!
            self.update_particles_with_odom(msg)    # update based on odometry
            if self.last_projected_stable_scan:
                last_projected_scan_timeshift = deepcopy(self.last_projected_stable_scan)
                last_projected_scan_timeshift.header.stamp = msg.header.stamp
                self.scan_in_base_link = self.tf_listener.transformPointCloud("base_link", last_projected_scan_timeshift)

            self.update_particles_with_laser(msg)   # update based on laser scan
            self.update_robot_pose(msg.header.stamp)                # update robot's pose
            self.resample_particles()               # resample particles to focus on areas of high density
        # publish particles (so things like rviz can see them)
        self.publish_particles(msg)

if __name__ == '__main__':
    n = ParticleFilter()
    r = rospy.Rate(5)

    while not(rospy.is_shutdown()):
        # in the main loop all we do is continuously broadcast the latest map to odom transform
        n.transform_helper.send_last_map_to_odom_transform()
        r.sleep()

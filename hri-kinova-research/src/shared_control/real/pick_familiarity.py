#!/usr/bin/env python3

import math
import time

import rospy
import torch
import numpy as np
import matplotlib.pyplot as plt
import torch.nn.functional as F

from example_full_arm_movement import ExampleFullArmMovement
from user_study import UserStudyExperiment
from sensor_msgs.msg import PointCloud2, PointField, PointCloud
from kortex_driver.msg import Twist
from KinovaRobot import KinovaRobot
from ActuatorModel import ActuatorModel
from sensor_msgs.msg import Joy, JointState
from kortex_api.autogen.client_stubs.BaseClientRpc import BaseClient
from kortex_api.autogen.messages import Base_pb2, BaseCyclic_pb2, Common_pb2
from kortex_api.autogen.client_stubs.BaseCyclicClientRpc import BaseCyclicClient
from kortex_driver.msg import BaseCyclic_Feedback, Twist, CartesianSpeed, CartesianReferenceFrame, ActionType, \
    ActionNotification, ConstrainedPose, ActionEvent, Finger, GripperMode, ControllerNotification, ControllerType, \
    Waypoint, AngularWaypoint, WaypointList, TwistCommand
from kortex_driver.srv import Base_ClearFaults, ExecuteAction, ExecuteActionRequest, SetCartesianReferenceFrameRequest, \
    ValidateWaypointList, OnNotificationActionTopic, OnNotificationActionTopicRequest, SendGripperCommand, \
    SendGripperCommandRequest, Stop, ReadActionRequest, ReadAction, SetCartesianReferenceFrame, SendTwistCommand, \
    SendTwistCommandRequest, SendTwistJoystickCommand, SendTwistJoystickCommandRequest

"""
0 - TELEOPERATION
1 - SHARED AUTONOMY
2 - VISION SA
"""
MODE = 1
"""
1. Reformat code to check for empty lists.
2. Adjusted Z calculation
3. Test
4. Launch File
5. Parameterize

"""


class AssistiveTeleoperation:
    def __init__(self):
        self.Y_pressed = False
        self.first_obj_grasped = False
        self.adjusted_z = None
        self.start_time = None
        self.end_time = None
        self.subscriber = rospy.Subscriber('/clusters', PointCloud, self.centroid_callback)
        self.robot_name = 'my_gen3'
        self.starting_state = Twist()
        self.uh = Twist()
        self.last_nonzero_uh = Twist()

        self.current_goal = None
        self.human_input = False
        self.next_user_state = None
        self.next_robot_state = None
        self.current_robot_state = None
        self.gripper_closed = False
        self.intermediate_position_reached = True
        self.num_times_gripper_opened = 0
        self.curr_pos = []
        self.positions = []
        self.goal_predications = []
        self.trajectory_user_inputs = []
        self.ee_position = np.array([0.0, 0.0, 0.0])
        self.ee_pose = np.array([0.0, 0.0, 0.0])

        self.fourth_pose = np.array([176.919, -0.889, 89.402])  # Tennis Ball Can

        self.Z_is_updated = False
        self.cumulative_input = 0
        self.final_score = 0

        self.goal_pos = []
        self.obj_pos = []

        self.list_goal_poses = [self.fourth_pose]
        self.confidences = []

        # Joystick Feedback
        self.subscribe_custom_joy_node()
        # self.subscribe_joy_feedback()
        # Action feedback

        self.jointfeedback = rospy.Subscriber('/' + self.robot_name + "/joint_states", JointState,
                                              self.action_feedback_callback, buff_size=1)
        # Base feedback
        self.basefeedback = rospy.Subscriber("/" + self.robot_name + "/base_feedback", BaseCyclic_Feedback,
                                             self.base_feedback_callback, buff_size=1)

        # Services
        send_gripper_command_full_name = '/' + self.robot_name + '/base/send_gripper_command'
        rospy.wait_for_service(send_gripper_command_full_name)
        self.gripper_command = rospy.ServiceProxy(send_gripper_command_full_name, SendGripperCommand)

        send_twist_command_full_name = 'my_gen3/base/send_twist_joystick_command'
        rospy.wait_for_service(send_twist_command_full_name)
        self.send_twist_command = rospy.ServiceProxy(send_twist_command_full_name, SendTwistJoystickCommand)
        self.alpha_history = []

    def disable_centroid_updates(self, event):
        print("DONE WITH CENTROID UPDATES")
        self.subscriber.unregister()

    def centroid_callback(self, centroid_msg):
        # self.obj_pos = []

        # print("Obj Pos List", self.obj_pos)
        if MODE == 1:
            # first_obj_pos = np.array([0.82454109, 0.3518613, 0.04850895])
            first_obj_pos = np.array([0.644, 0.351, 0.075])
            # first_obj_pos = np.array([0.63, 0.463, 0.122])
            # second_obj_pos = np.array([0.647, 0.24, 0.056])
            first_obj_pose = np.array([0, 0, 0])

            self.obj_pos = [first_obj_pos]
            self.list_goal_poses = [first_obj_pose]  # Add pose here
            # self.goal_pos = [self.first_goal_pos, self.second_goal_pos, self.third_goal_pos]

        elif MODE == 2:
            if self.ee_position[0] > 0.45:
                return

            # if self.ee_position[1] < 0.10:
            #     return

            obj_pos = []
            for point in centroid_msg.points:
                print(point)
                centroid = np.array([point.x + 0.025, point.y - 0.01, point.z])
                if point.y > 0.25:  # Adjust this threshold based on enviornment.
                    obj_pos.append(centroid)

            if self.intermediate_position_reached is False and len(obj_pos) == 0:
                return

            self.intermediate_position_reached = True
            self.obj_pos = obj_pos


        # print("Obj Pos ", self.obj_pos)

    def plot_goal_confidences(self, ax):
        plt.clf()
        latest_confidences = self.confidences
        ax.bar(range(len(self.current_goal)), latest_confidences, color='blue')  # Create bar graph
        ax.set_xlabel('Goals')
        ax.set_ylabel('Confidence')
        ax.set_title('Confidence for Each Goal')
        ax.set_xticks(range(len(self.current_goal)))
        ax.set_xticklabels(['Goal 1', 'Goal 2', 'Goal 3', 'Goal 4'])
        plt.legend()
        plt.draw()
        plt.pause(0.1)  # Pause to update the plot

    def plot_alpha_values(self, ax):

        latest_confidences = self.confidences
        plt.cla()
        ax.plot(range(len(self.alpha_history[-100:])), self.alpha_history[-100:], color='blue', label="Robot Control")
        ax.plot(range(len(self.alpha_history[-100:])), 1 - np.array(self.alpha_history[-100:]), color='red',
                label="Human Control")
        ax.set_xlabel('Time')
        ax.set_ylabel('Control Weighting')
        ax.set_title('Human vs Robot Control over Time')
        ax.set_ylim(0, 1)
        plt.draw()

        plt.pause(0.1)  # Pause to update the plot

    def twist_to_array(self, twist):
        return np.array([twist.linear_x, twist.linear_y, twist.linear_z])

    # def subscribe_joy_feedback(self):
    #     rospy.loginfo("Subscribing to joy node")
    #     rospy.Subscriber("/joy", Joy, self.controller_callback)

    def subscribe_custom_joy_node(self):
        rospy.loginfo("Subscribing to Custom Joy Node")
        rospy.Subscriber("/custom_joy_node", Joy, self.controller_callback)

    def twist_command(self, x, y, z, angle_x=None, angle_y=None, angle_z=None):
        twist = Twist()
        twist.linear_x = float(x)
        twist.linear_y = float(y)
        twist.linear_z = float(z)

        # Angular Velocities
        # NOTE: In the range [0, 1], in rad/s. 0.5 is fast, keep these numbers low
        if angle_x is not None:
            twist.angular_x = float(angle_x)
        if angle_y is not None:
            twist.angular_y = float(angle_y)
        if angle_z is not None:
            twist.angular_z = float(angle_z)

        twist_command = TwistCommand()
        twist_command.duration = 0
        twist_command.reference_frame = 1
        twist_command.twist = twist

        # rospy.loginfo("Sending pose")
        try:
            self.send_twist_command(twist_command)

        except rospy.ServiceException:
            rospy.logerr("Failed to send pose")
            return False

    def controller_callback(self, data):
        controller_position = data.axes
        controller_buttons = data.buttons
        if controller_position[5] == -1.0 and self.gripper_closed:
            self.send_gripper_command(0.0)
            self.gripper_closed = False
            # self.obj_pos = [np.array([0.40, 0.334, 0.087])]
            self.intermediate_position_reached = False
            self.end_time = time.time()
            self.final_score = self.cumulative_input
            self.last_nonzero_uh = Twist()

            for idx, goal in enumerate(self.goal_pos):
                if np.linalg.norm(self.ee_position - goal) < 0.05:
                    print(f"POPPED {idx, goal}")
                    self.goal_pos.pop(idx)

        elif controller_position[2] == -1.0 and not self.gripper_closed:
            self.send_gripper_command(0.40)
            self.gripper_closed = True
            self.last_nonzero_uh = Twist()

        if controller_buttons[3] == 1 and not self.gripper_closed:
            self.Y_pressed = True
            print("RESET ROBOT")

        # if controller_buttons[0]:
        #     if not self.gripper_closed:
        #         self.send_gripper_command(0.5)
        #         self.gripper_closed = True
        #         # self.obj_pos = []
        #     else:
        #         self.send_gripper_command(0.0)
        #         self.gripper_closed = False
        #         self.num_times_gripper_opened += 1

        twist = Twist()

        linear_components = np.array([
            float(controller_position[1]),  # Controller X
            float(controller_position[0]),  # Controller Y
            float(controller_position[4]),  # Controller Z
        ])
        # Normalize Input
        if np.linalg.norm(linear_components) > 1e-9:
            linear_components /= np.linalg.norm(linear_components)

        twist.linear_x = linear_components[0]
        twist.linear_y = linear_components[1]
        twist.linear_z = linear_components[2]
        self.uh = twist

        # Gripper Control
        # controller_position = data.axes

        # return True

        if controller_position[1] == 0 and controller_position[0] == 0 and controller_position[4] == 0:
            self.human_input = False

        else:
            self.human_input = True
            self.last_nonzero_uh = twist

    def action_feedback_callback(self, feedback):
        return str(feedback.position)

    def send_gripper_command(self, value):
        # Initialize the request
        req = SendGripperCommandRequest()
        finger = Finger()
        finger.finger_identifier = 0
        finger.value = value
        req.input.gripper.finger.append(finger)
        req.input.mode = GripperMode.GRIPPER_POSITION
        # Call the service
        try:
            self.gripper_command(req)
            return True
        except rospy.ServiceException:
            rospy.logerr("Failed to call SendGripperCommand")
            return False
        # else:
        #     time.sleep(0.5)
        #     return True

    def base_feedback_callback(self, feedback):
        get_state = ActuatorModel()
        get_state.position_0 = feedback.actuators[0].position
        get_state.position_1 = feedback.actuators[1].position
        get_state.position_2 = feedback.actuators[2].position
        get_state.position_3 = feedback.actuators[3].position
        get_state.position_4 = feedback.actuators[4].position
        get_state.position_5 = feedback.actuators[5].position
        get_state.position_6 = feedback.actuators[6].position

        self.positions.append(get_state)
        self.curr_pos = get_state.get_position()

        # Set the end effector position for the class
        self.ee_position[0] = feedback.base.tool_pose_x
        self.ee_position[1] = feedback.base.tool_pose_y
        self.ee_position[2] = feedback.base.tool_pose_z

        # print("EE",self.ee_position[0], self.ee_position[1], self.ee_position[2])
        # Set the end effector pose for the class
        self.ee_pose[0] = feedback.base.tool_pose_theta_x
        self.ee_pose[1] = feedback.base.tool_pose_theta_y
        self.ee_pose[2] = feedback.base.tool_pose_theta_z

    def compute_alpha(self, confidence, method, alpha_min=0.0, alpha_max=0.8):
        if method == 'linear':
            alpha = alpha_min + (alpha_max - alpha_min) * confidence
        if method == 'sigmoid':
            k = 10  # steepness of the curve
            alpha = alpha_min + (alpha_max - alpha_min) / (1 + math.exp(-k * confidence - 0.5))

        if method == 'piecewise':
            if confidence < 0.4:
                alpha = alpha_min
            elif 0.4 <= confidence < 0.7:
                theta = (alpha_max - alpha_min) / 0.3  # change in alpha / change in confidence = SLOPE
                offset = alpha_min - (theta * 0.4)  # y-intercept
                alpha = theta * confidence + offset  # should give y=mx+c
            else:
                alpha = alpha_max
        alpha = max(min(alpha, alpha_max), alpha_min)  # making sure alpha stays between 0 and 1
        return alpha

    def compute_ur_for_all_goals(self, current_position, current_pose):
        ur_list = list()
        for i, goal in enumerate(self.current_goal):
            direction_vector = goal - current_position
            dir_norm = np.linalg.norm(direction_vector)
            # print("Direction Norm:", dir_norm)
            ur = direction_vector / dir_norm
            ##
            if dir_norm <= 0.02:
                ur *= 0
                ur_list.append(np.zeros(6))
                continue
            ##

            desired_pose = self.list_goal_poses[0]
            angular_displacement = desired_pose - current_pose
            angular_norm = np.linalg.norm(angular_displacement)
            angular_velocity = angular_displacement / angular_norm

            ####
            if angular_norm < 0.4:
                angular_velocity *= 0
            ####

            ur_list.append(np.concatenate((ur, angular_velocity)))

        # print("UR List: ", ur_list)
        return ur_list

    def compute_confidence(self, ur, predicted_goal_index, confidence_min=0.0, confidence_max=1.0):
        if not self.current_goal:
            # print("list_goals empty")
            return 0.0

        predicted_goal = self.current_goal[predicted_goal_index]
        dist_to_goal = np.linalg.norm(self.ee_position - predicted_goal)

        w1 = 0.3
        w2 = 0.7

        human_inp = np.array(
            [self.last_nonzero_uh.linear_x, self.last_nonzero_uh.linear_y, self.last_nonzero_uh.linear_z])

        term1 = w1 * np.dot(human_inp, ur[0:3])
        term2 = w2 * math.exp(-dist_to_goal)

        confidence = term1 + term2
        return max(min(confidence, confidence_max), confidence_min)

    def get_robot_command(self, inferred_goal):
        current_position = self.ee_position
        goal_position = self.current_goal[inferred_goal]

        # print("Goal Position", goal_position)
        # print("Current Position", current_position)

        direction_vector = goal_position - current_position
        ur = direction_vector / np.linalg.norm(direction_vector)

        return ur

    def input_magnitude(self, uh):
        displacement_h = np.array([uh.linear_x, uh.linear_y, uh.linear_z, 0.0, 0.0, 0.0])
        return np.linalg.norm(displacement_h)

    def blend_inputs(self, uh, ur, predicted_goal_index):
        # print(ur)

        # The human's desired control input
        displacement_h = np.array([uh.linear_x, uh.linear_y, uh.linear_z, 0.0, 0.0, 0.0])
        confidence = self.compute_confidence(ur, predicted_goal_index)

        if not self.obj_pos:
            alpha = 0
        else:
            if np.linalg.norm(displacement_h) > 1e-9:
                alpha = self.compute_alpha(confidence, method='piecewise')
            else:
                alpha = 1.0 if confidence > 0.4 else 0
            # alpha = self.compute_alpha(confidence, method='piecewise')

        # A blending of the two
        # return alpha * displacement_h + (1 - alpha) * ur, alpha
        # alpha = 1
        return alpha * ur + (1 - alpha) * displacement_h, alpha

    def get_curr_pos(self):
        if len(self.positions) == 0:
            return 0

        actuatorModel = ActuatorModel()
        curr_pos = self.positions.pop(len(self.positions) - 1)
        joint_angles = actuatorModel.setActuatorData(curr_pos.position_0, curr_pos.position_1,
                                                     curr_pos.position_2, curr_pos.position_3,
                                                     curr_pos.position_4, curr_pos.position_5,
                                                     curr_pos.position_6)

        return joint_angles

    def calculate_distance_from_goal(self, current_position):
        distances = [np.linalg.norm(goal - current_position) for goal in self.current_goal]
        return distances

    def infer_goal(self, ur_list):
        raw_confidences = [self.compute_confidence(ur_list[i], i) for i in range(len(ur_list))]
        # print("raw-confidences", raw_confidences)
        # if not raw_confidences:
        #     print("No raw confidences")
        #     return 0, 0.0
        softmax_confidences = F.softmax(torch.tensor(raw_confidences), dim=0)
        confidences = softmax_confidences.tolist()
        inferred_goal_index = torch.argmax(softmax_confidences).item()
        return inferred_goal_index, confidences[inferred_goal_index]

    def cartesian_to_joint_command(self, base, r3_displacement):
        if len(self.curr_pos) == 0:
            print("Not initialized yet, waiting...")
            return

        desired_action = Base_pb2.Action()
        desired_action.reach_pose.target_pose.x = self.ee_position[0] + r3_displacement[0]
        desired_action.reach_pose.target_pose.y = self.ee_position[1] + r3_displacement[1]
        desired_action.reach_pose.target_pose.z = self.ee_position[2] + r3_displacement[2]
        desired_action.reach_pose.target_pose.theta_x = self.ee_pose[0]
        desired_action.reach_pose.target_pose.theta_y = self.ee_pose[1]
        desired_action.reach_pose.target_pose.theta_z = self.ee_pose[2]

        return desired_action

    def close_gripper(self):
        self.send_gripper_command(0.65)
        self.gripper_closed = True

    def open_gripper(self):
        self.send_gripper_command(0)  # 0 is fully open
        self.gripper_closed = False

    def main(self):
        reset = ExampleFullArmMovement()
        self.start_time = time.time()
        try:
            robot = KinovaRobot()
            success = robot.is_init_success
            if success:
                success &= robot.clear_faults()
            if success:
                success &= robot.subscribe_base_feedback()
        except Exception as e:
            print(e)
            success = False

        rospy.set_param("is_initialized", success)
        if not success:
            rospy.logerr("The robot initialization encountered an error.")
        else:
            rospy.loginfo("The robot initialization executed without fail.")

        rate = rospy.Rate(10)

        try:
            while not rospy.is_shutdown():
                self.cumulative_input += self.input_magnitude(self.uh)

                # IN ANY MODE, IF Y IS PRESSED, RESET THE ROBOT
                if self.Y_pressed:
                    print("Triggering Reset...")
                    self.twist_command(-0.55, 0, 0.45, angle_x=0, angle_y=0, angle_z=0)
                    time.sleep(3)
                    self.twist_command(0, 0, 0, angle_x=0, angle_y=0, angle_z=0)
                    time.sleep(0.5)
                    # reset.example_send_joint_angles([8.863, 46.487, 187.919, 248.36, 15.302, 69.849, 78.489])
                    # reset.example_send_joint_angles([7.867, 45.132, 176.316, 264.945, 359.8, 58.086, 70.595])
                    # time.sleep(5)
                    reset.example_send_joint_angles([287.87, 79.29, 152.622, 253.799, 281.052, 116.668, 90.08])
                    # reset.df_send_joint_angles(df)
                    time.sleep(8.0)
                    reset.example_clear_faults()
                    self.Y_pressed = False
                    time.sleep(0.2)

                # TELEOPERATION
                elif MODE == 0:
                    self.twist_command(self.uh.linear_x * 0.5, self.uh.linear_y * 0.5, self.uh.linear_z * 0.5)

                # VISION BASED SHARED AUTONOMY
                else:
                    if self.gripper_closed:
                        self.current_goal = self.goal_pos
                    else:
                        self.current_goal = self.obj_pos

                    ur_list = self.compute_ur_for_all_goals(self.ee_position, self.ee_pose)
                    goal_locations_known = len(ur_list) > 0

                    # print("UR LIST:", ur_list)
                    if goal_locations_known:
                        inferred_goal, confidence = self.infer_goal(ur_list)

                        self.goal_predications.append(inferred_goal)

                        blended_commmand, alpha = self.blend_inputs(self.uh, ur_list[inferred_goal],
                                                                    predicted_goal_index=inferred_goal)
                    else:
                        # If no goals, set ur = zeros
                        blended_commmand, alpha = self.blend_inputs(self.uh, np.zeros(6),
                                                                    predicted_goal_index=-1)

                    blended_commmand[0] *= 1
                    blended_commmand[1] *= 1
                    blended_commmand[2] *= 15 if self.gripper_closed else 3
                    # alpha = self.compute_alpha(confidence, method='piecewise')
                    if np.linalg.norm(blended_commmand[0:3]) > 1:
                        blended_commmand[0:3] /= np.linalg.norm(blended_commmand[0:3])
                    blended_commmand *= 0.5
                    self.alpha_history.append(alpha)

                    blended_commmand[4] *= -1
                    blended_commmand[5] *= -1

                    self.twist_command(blended_commmand[0], blended_commmand[1], blended_commmand[2])
                    # angle_x=blended_commmand[3], angle_y=blended_commmand[4],
                    # angle_z=blended_commmand[5])

                    if goal_locations_known and self.gripper_closed and not self.Z_is_updated:
                        self.adjusted_z = self.ee_position[2]
                        for i in range(len(self.goal_pos)):
                            self.goal_pos[i][2] = 0.257 + self.adjusted_z
                        # self.first_goal_pos = np.array([0.84904802, -0.39625537, 0.34892158 + self.adjusted_z])
                        # self.second_goal_pos = np.array([0.82977957, -0.19742057, 0.35007457 + self.adjusted_z])
                        # self.third_goal_pos = np.array([0.83475798, -0.00684316, 0.3475138 + self.adjusted_z])
                        self.Z_is_updated = True

                        # print("Z Updated!", self.first_goal_pos, self.second_goal_pos)

                    if not self.gripper_closed:
                        self.Z_is_updated = False
                        for i in range(len(self.goal_pos)):
                            self.goal_pos[i][2] = 0.28

                    print("Goal Pos Len", len(self.goal_pos))

                # EndIf
                # print(f"Human Input: ", {self.cumulative_input})
                # print(f"EE position: ", self.ee_position)
                rate.sleep()


        except rospy.ROSInterruptException:
            print(rospy.ROSInterruptException)
            self.twist_command(0, 0, 0, angle_x=0, angle_y=0, angle_z=0)
        finally:
            if self.end_time is None:
                print(
                    "WARNING: Experiment stopping time was not assigned at runtime. The following time represents the duration of execution, but may not align with the task.")
                self.end_time = time.time()
                self.final_score = self.cumulative_input
            total_time = (self.end_time - self.start_time)
            print(f"Experiment Time: {total_time}s")
            print(f"Total Human Input: {self.final_score}")
            UserStudyExperiment.record_result("Reaching", MODE, task_duration=total_time, input_magnitude=self.final_score)

            self.twist_command(0, 0, 0, angle_x=0, angle_y=0, angle_z=0)


if __name__ == "__main__":
    rospy.init_node('pick_and_place_familiar')
    # MODE = rospy.get_param('/pick_and_place_vision/~MODE')
    asst_teleop = AssistiveTeleoperation()
    asst_teleop.main()

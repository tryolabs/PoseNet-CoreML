import json
import time
import struct
import tensorflow as tf
import cv2
import numpy as np
import os
import heapq
import yaml
import random

from utils import get_image_coords, within_radius_of_corresponding_point, convert_to_cv2_point
from decode_pose import decode_pose, get_instance_score
from keypoints import pose_chain, part_name_to_id_map


# GLOBALS
MANIFEST_FILENAME = "manifest.json"
CONFIG_PATH = "converter/config.yaml"
WEIGHTS_PATH = "converter/waits"
INPUT_IMAGE_PATH = "converter/images/tennis_in_crowd.jpg"
OUTPUT_IMAGE_PATH = "output.jpg"

K_LOCAL_MAXIMUM_RADIUS = 1  # TODO: WHAT? They use 1 in implementation but not in paper
PERSON_SCORE_THRESHOLD = 0.4
PART_SCORE_THRESHOLD = 0.5
NMS_RADIUS = 20  # TODO: Not used?? the same as K_LOCAL_MAXIMUM_RADIUS?
OUTPUT_STRIDE = 16
MAX_POSE_DETECTIONS = float('inf')


def toOutputStridedLayers(convolutionDefinition, outputStride):
    currentStride = 1
    rate = 1
    blockId = 0
    buff = []
    for _a in convolutionDefinition:
        convType = _a[0]
        stride = _a[1]

        if (currentStride == outputStride):
            layerStride = 1
            layerRate = rate
            rate *= stride
        else:
            layerStride = stride
            layerRate = 1
            currentStride *= stride

        buff.append({
            'blockId': blockId,
            'convType': convType,
            'stride': layerStride,
            'rate': layerRate,
            'outputStride': currentStride
        })
        blockId += 1

    return buff


def read_imgfile(path, width, height):
    img = cv2.imread(path)
    img = cv2.resize(img, (width, height))
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    img = img.astype(float)
    img = img * (2.0 / 255.0) - 1.0
    return img


def convToOutput(mobileNetOutput, outputLayerName):
    w = tf.nn.conv2d(mobileNetOutput, weights(outputLayerName), [1, 1, 1, 1], padding='SAME')
    w = tf.nn.bias_add(w, biases(outputLayerName), name=outputLayerName)
    return w


def conv(inputs, stride, blockId):
    return tf.nn.relu6(
        tf.nn.conv2d(inputs, weights("Conv2d_" + str(blockId)), stride, padding='SAME')
        + biases("Conv2d_" + str(blockId))
    )


def weights(layerName):
    return variables["MobilenetV1/" + layerName + "/weights"]['x']


def biases(layerName):
    return variables["MobilenetV1/" + layerName + "/biases"]['x']


def depthwiseWeights(layerName):
    return variables["MobilenetV1/" + layerName + "/depthwise_weights"]['x']


def separableConv(inputs, stride, blockID, dilations):
    if dilations is None:
        dilations = [1, 1]

    dwLayer = "Conv2d_" + str(blockID) + "_depthwise"
    pwLayer = "Conv2d_" + str(blockID) + "_pointwise"

    w = tf.nn.depthwise_conv2d(
        inputs, depthwiseWeights(dwLayer), stride, 'SAME', rate=dilations, data_format='NHWC'
    )
    w = tf.nn.bias_add(w, biases(dwLayer))
    w = tf.nn.relu6(w)

    w = tf.nn.conv2d(w, weights(pwLayer), [1, 1, 1, 1], padding='SAME')
    w = tf.nn.bias_add(w, biases(pwLayer))
    w = tf.nn.relu6(w)

    return w


def decode_multiple_poses(heatmap_scores, offsets, displacements_fwd, displacements_bwd,
                          output_stride, max_pose_detections, person_score_threshold,
                          part_score_threshold=0.5, nms_radius=20):
    poses = []
    queue = build_part_with_score_queue(
        part_score_threshold, K_LOCAL_MAXIMUM_RADIUS, heatmap_scores
    )
    squared_nms_radius = nms_radius ** 2

    while len(poses) < max_pose_detections and len(queue) != 0:
        root = heapq.heappop(queue)
        root = (root[0], root[2])  # TODO: Temporary solution for priority queue ties
        root_image_coords = get_image_coords(root[1], output_stride, offsets)

        if within_radius_of_corresponding_point(poses, squared_nms_radius,
                                                root_image_coords, root[1]['keypoint_id']):
            continue

        keypoints = decode_pose(root, heatmap_scores, offsets, output_stride,
                                displacements_fwd, displacements_bwd)

        score = get_instance_score(poses, squared_nms_radius, keypoints)
        if score > person_score_threshold:
            poses.append({'keypoints': keypoints, 'score': score})

    return poses


def build_part_with_score_queue(part_score_threshold, local_max_radius, heatmap_scores):
    height, width, num_keypoints = heatmap_scores.shape
    queue = []  # We'll use a reversed heapq to implement a max heap
    counter = 0  # Used to resolve ties when two heatmap scores are equal
                 # TODO: Temporary solution for priority queue ties
    for heatmap_y in range(height):
        for heatmap_x in range(width):
            for keypoint_id in range(num_keypoints):
                score = heatmap_scores[heatmap_y, heatmap_x, keypoint_id]  # TODO check index order

                # Only consider parts with score greater or equal to threshold as root candidates.
                if score < part_score_threshold:
                    continue

                # Only consider keypoints whose score is maximum in a local window.
                if score_is_maximum_in_local_window(keypoint_id, score, heatmap_y, heatmap_x,
                                                    local_max_radius, heatmap_scores):
                    # For some reason python only allows min heaps (not max heaps)
                    # so I negate the score 🤮
                    heapq.heappush(
                        queue,
                        (-score, counter, {'heatmap_y': heatmap_y,
                                           'heatmap_x': heatmap_x,
                                           'keypoint_id': keypoint_id})
                    )
                    counter += 1
    return queue


def score_is_maximum_in_local_window(keypoint_id, score, heatmap_y, heatmap_x,
                                     local_max_radius, heatmap_scores):
    # TODO: I could easily vectorize this whole function.
    #       Don't know if it will be faster, due to break saving me iterations though.
    height, width, _ = heatmap_scores.shape  # We recieve a single layer

    local_maximum = True
    y_start = max(heatmap_y - local_max_radius, 0)
    y_end = min(heatmap_y + local_max_radius + 1, height)
    for y_current in range(y_start, y_end):
        # TODO: these x_start, x_end, definitions should be defined outside of this loop right?
        x_start = max(heatmap_x - local_max_radius, 0)
        x_end = min(heatmap_x + local_max_radius + 1, width)
        for x_current in range(x_start, x_end):
            if heatmap_scores[y_current, x_current, keypoint_id] > score:
                local_maximum = False
                break
        if not local_maximum:
            break
    return local_maximum


# Set up network configuration
with open(CONFIG_PATH, "r+") as f:
    cfg = yaml.load(f)
checkpoints = cfg['checkpoints']
imageSize = cfg['imageSize']
chk = cfg['chk']
outputStride = OUTPUT_STRIDE
chkpoint = checkpoints[chk]

if chkpoint == 'mobilenet_v1_050':
    mobileNetArchitectures = cfg['mobileNet50Architecture']
elif chkpoint == 'mobilenet_v1_075':
    mobileNetArchitectures = cfg['mobileNet75Architecture']
else:
    mobileNetArchitectures = cfg['mobileNet100Architecture']

width = imageSize
height = imageSize


# Load weights from harddrive into `variables` list
with open(os.path.join(WEIGHTS_PATH, chkpoint, MANIFEST_FILENAME)) as f:
    variables = json.load(f)

for x in variables:
    filename = variables[x]["filename"]
    byte = open(os.path.join(WEIGHTS_PATH, chkpoint, filename), 'rb').read()
    fmt = str(int(len(byte) / struct.calcsize('f'))) + 'f'
    d = struct.unpack(fmt, byte)
    # d = np.array(d,dtype=np.float32)
    d = tf.cast(d, tf.float32)
    d = tf.reshape(d, variables[x]["shape"])
    variables[x]["x"] = tf.Variable(d, name=x)

image = tf.placeholder(tf.float32, shape=[1, imageSize, imageSize, 3], name='image')
x = image

# Define base network and load its weights
rate = [1, 1]
layers = toOutputStridedLayers(mobileNetArchitectures, outputStride)
with tf.variable_scope(None, 'MobilenetV1'):
    for m in layers:
        stride = [1, m['stride'], m['stride'], 1]
        rate = [m['rate'], m['rate']]
        if (m['convType'] == "conv2d"):
            x = conv(x, stride, m['blockId'])
        elif (m['convType'] == "separableConv"):
            x = separableConv(x, stride, m['blockId'], rate)

# Define personlab layers and load their weights
heatmaps = convToOutput(x, 'heatmap_2')
offsets = convToOutput(x, 'offset_2')
displacements_fwd = convToOutput(x, 'displacement_fwd_2')
displacements_bwd = convToOutput(x, 'displacement_bwd_2')
heatmaps = tf.sigmoid(heatmaps, 'heatmap')

# Define init operations
init = tf.global_variables_initializer()

with tf.Session() as sess:
    # Initialize network
    sess.run(init)

    input_image = read_imgfile(INPUT_IMAGE_PATH, width, height)
    input_image = np.array(input_image, dtype=np.float32)
    for _ in range(10):
        start = time.time()
        # Resize input
        input_image = input_image.reshape(1, width, height, 3)
        after_resize = time.time()

        # Run input through model
        # Offsets and displacements have their x's and y's concatenated in the same dim
        heatmaps_result, offsets_result, displacements_fwd_result, displacements_bwd_result = sess.run(  # noqa
            [heatmaps, offsets, displacements_fwd, displacements_bwd], feed_dict={image: input_image})  # noqa
        after_detect = time.time()

        # Generate poses from model's output
        poses = decode_multiple_poses(
            heatmaps_result[0], offsets_result[0], displacements_fwd_result[0],
            displacements_bwd_result[0], OUTPUT_STRIDE, MAX_POSE_DETECTIONS, PERSON_SCORE_THRESHOLD,
            PART_SCORE_THRESHOLD, NMS_RADIUS
        )
        after_decode = time.time()
        print(
            f"Resize: {after_resize - start: .4f}, "
            f"Detect: {after_detect - after_resize: .4f}, "
            f"Decode: {after_decode - after_detect:.4f} "
            f"People: {len(poses)}"
        )

    # Convert to cv2 format (is this necessary?)
    input_image_cv2 = cv2.imread(INPUT_IMAGE_PATH)

    if False:
        # Draw heatmap
        heatmap_index = 1  # Change this value to change what to draw
        heatmaps_img = (heatmaps_result * 255).astype(np.uint8)[:, :, :, heatmap_index][0]
        resized_heatmaps_img = cv2.resize(
            heatmaps_img, tuple(image.shape[1:3].as_list()), interpolation=cv2.INTER_CUBIC
        )
        cv2.imshow('image', (
            0.7 * np.expand_dims(resized_heatmaps_img, axis=2) + 0.3 * input_image_cv2
        ).astype(np.uint8))
        cv2.waitKey(0)

    # Draw keypoints
    child_to_parent_map = {child: part_name_to_id_map[parent] for parent, child in pose_chain}
    for person in poses:
        color = (255, 255, 255)
        color = (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
        width = 2
        for keypoint in person['keypoints']:
            if keypoint['part'] == 'nose':
                cv2.circle(
                    input_image_cv2,
                    convert_to_cv2_point(keypoint['position']),
                    width, color
                )
                text_position = (int(round(keypoint['position']['x'])),
                                 int(round(keypoint['position']['y'])) - 10)
                cv2.putText(input_image_cv2, f"{person['score']:.2f}", text_position,
                            cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1, cv2.LINE_AA)
            else:
                parent_point_idx = child_to_parent_map[keypoint['part']]
                parent_point = person['keypoints'][parent_point_idx]
                cv2.line(
                    input_image_cv2,
                    convert_to_cv2_point(keypoint['position']),
                    convert_to_cv2_point(parent_point['position']),
                    color,
                    width
                )

    resized_image = cv2.resize(input_image_cv2, (2000, 2000))
    cv2.imshow('image', resized_image)
    # cv2.imwrite('lol.png', input_image_cv2)
    cv2.waitKey(0)

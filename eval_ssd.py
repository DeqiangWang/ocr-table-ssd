import torch
from vision.ssd.vgg_ssd import create_vgg_ssd, create_vgg_ssd_predictor
from vision.ssd.mobilenetv1_ssd import create_mobilenetv1_ssd, create_mobilenetv1_ssd_predictor
from vision.ssd.mobilenetv1_ssd_lite import create_mobilenetv1_ssd_lite, create_mobilenetv1_ssd_lite_predictor
from vision.ssd.squeezenet_ssd_lite import create_squeezenet_ssd_lite, create_squeezenet_ssd_lite_predictor
from vision.datasets.voc_dataset import VOCDataset
from vision.datasets.open_images import OpenImagesDataset
from vision.utils import box_utils, measurements
from vision.utils.misc import str2bool, Timer
import argparse
import pathlib
import numpy as np
import logging
import sys
import random
from vision.ssd.mobilenet_v2_ssd_lite import create_mobilenetv2_ssd_lite, create_mobilenetv2_ssd_lite_predictor
from vision.ssd.imJnet_ssd_lite import create_imJnet_ssd_lite
from vision.ssd.imJnet_ssd_lite import create_imJnet_ssd_lite_predictor
import cv2
import string
import imutils
parser = argparse.ArgumentParser(description="SSD Evaluation on VOC Dataset.")
parser.add_argument('--net', default="jnet-ssd-lite",
                    help="The network architecture, it should be of mb1-ssd, mb1-ssd-lite, mb2-ssd-lite, jnet-ssd-lite or vgg16-ssd.")
# model_log/jnet-ssd-lite-Epoch-210-Loss-0.6506194928113151.pth
parser.add_argument("--trained_model", default= 'model_log/jnet-ssd-lite-Epoch-220-Loss-0.7154099260057721.pth', type=str)

parser.add_argument("--dataset_type", default="voc", type=str,
                    help='Specify dataset type. Currently support voc and open_images.')
parser.add_argument("--dataset", default='/media/handsome/backupdata/hanson/ocr_table_dataset_v2/Cropped', type=str, help="The root directory of the VOC dataset or Open Images dataset.")
parser.add_argument("--label_file", default='model_log/voc-model-labels.txt' ,type=str, help="The label file path.")
parser.add_argument("--use_cuda", type=str2bool, default=True)
parser.add_argument("--use_2007_metric", type=str2bool, default=True)
parser.add_argument("--nms_method", type=str, default="hard")
parser.add_argument("--iou_threshold", type=float, default=0.5, help="The threshold of Intersection over Union.")
parser.add_argument("--eval_dir", default="eval_results", type=str, help="The directory to store evaluation results.")
parser.add_argument('--mb2_width_mult', default=1.0, type=float,
                    help='Width Multiplifier for MobilenetV2')
args = parser.parse_args()
DEVICE = torch.device("cuda:0" if torch.cuda.is_available() and args.use_cuda else "cpu")
# DEVICE = torch.device("cpu")

def group_annotation_by_class(dataset):
    true_case_stat = {}
    all_gt_boxes = {}
    all_difficult_cases = {}
    for i in range(len(dataset)):
        image_id, annotation = dataset.get_annotation(i)
        gt_boxes, classes, is_difficult = annotation
        gt_boxes = torch.from_numpy(gt_boxes)
        for i, difficult in enumerate(is_difficult):
            class_index = int(classes[i])
            gt_box = gt_boxes[i]
            if not difficult:
                true_case_stat[class_index] = true_case_stat.get(class_index, 0) + 1

            if class_index not in all_gt_boxes:
                all_gt_boxes[class_index] = {}
            if image_id not in all_gt_boxes[class_index]:
                all_gt_boxes[class_index][image_id] = []
            all_gt_boxes[class_index][image_id].append(gt_box)
            if class_index not in all_difficult_cases:
                all_difficult_cases[class_index]={}
            if image_id not in all_difficult_cases[class_index]:
                all_difficult_cases[class_index][image_id] = []
            all_difficult_cases[class_index][image_id].append(difficult)

    for class_index in all_gt_boxes:
        for image_id in all_gt_boxes[class_index]:
            all_gt_boxes[class_index][image_id] = torch.stack(all_gt_boxes[class_index][image_id])
    for class_index in all_difficult_cases:
        for image_id in all_difficult_cases[class_index]:
            all_gt_boxes[class_index][image_id] = torch.tensor(all_gt_boxes[class_index][image_id])
    return true_case_stat, all_gt_boxes, all_difficult_cases


def compute_average_precision_per_class(num_true_cases, gt_boxes, difficult_cases,
                                        prediction_file, iou_threshold, use_2007_metric):
    with open(prediction_file) as f:
        image_ids = []
        boxes = []
        scores = []
        for line in f:
            t = line.rstrip().split(" ")
            image_ids.append(t[0])
            scores.append(float(t[1]))
            box = torch.tensor([float(v) for v in t[2:]]).unsqueeze(0)
            box -= 1.0  # convert to python format where indexes start from 0
            boxes.append(box)
        scores = np.array(scores)
        sorted_indexes = np.argsort(-scores)
        boxes = [boxes[i] for i in sorted_indexes]
        image_ids = [image_ids[i] for i in sorted_indexes]
        true_positive = np.zeros(len(image_ids))
        false_positive = np.zeros(len(image_ids))
        matched = set()
        for i, image_id in enumerate(image_ids):
            box = boxes[i]
            if image_id not in gt_boxes:
                false_positive[i] = 1
                continue

            gt_box = gt_boxes[image_id]
            ious = box_utils.iou_of(box, gt_box)
            max_iou = torch.max(ious).item()
            max_arg = torch.argmax(ious).item()
            if max_iou > iou_threshold:
                if difficult_cases[image_id][max_arg] == 0:
                    if (image_id, max_arg) not in matched:
                        true_positive[i] = 1
                        matched.add((image_id, max_arg))
                    else:
                        false_positive[i] = 1
            else:
                false_positive[i] = 1

    true_positive = true_positive.cumsum()
    false_positive = false_positive.cumsum()
    precision = true_positive / (true_positive + false_positive)
    recall = true_positive / num_true_cases
    print('precision',precision)
    print('recall' , recall)
    if use_2007_metric:
        return measurements.compute_voc2007_average_precision(precision, recall)
    else:
        return measurements.compute_average_precision(precision, recall)


def rotate_bound(image, angle):
    """from imutils module!"""
    # grab the dimensions of the image and then determine the
    # center
    (h, w) = image.shape[:2]
    (cX, cY) = (w / 2, h / 2)

    # grab the rotation matrix (applying the negative of the
    # angle to rotate clockwise), then grab the sine and cosine
    # (i.e., the rotation components of the matrix)
    M = cv2.getRotationMatrix2D((cX, cY), -angle, 1.0)
    cos = np.abs(M[0, 0])
    sin = np.abs(M[0, 1])

    # compute the new bounding dimensions of the image
    nW = int((h * sin) + (w * cos))
    nH = int((h * cos) + (w * sin))

    # adjust the rotation matrix to take into account translation
    M[0, 2] += (nW / 2) - cX
    M[1, 2] += (nH / 2) - cY

    # perform the actual rotation and return the image
    return cv2.warpAffine(image, M, (nW, nH))


def reverse_rotate(image, ori_shape, angle):
    """get reverse transform matrix!"""
    (h, w) = image.shape[:2]
    (cX, cY) = (w / 2., h / 2.)

    M = cv2.getRotationMatrix2D((cX, cY), -angle, 1.0)

    M[0, 2] += ori_shape[1] / 2 - cX
    M[1, 2] += ori_shape[0] / 2 - cY

    return M

if __name__ == '__main__':
    eval_path = pathlib.Path(args.eval_dir)
    eval_path.mkdir(exist_ok=True)
    timer = Timer()
    class_names = [name.strip() for name in open(args.label_file).readlines()]

    if args.dataset_type == "voc":
        dataset = VOCDataset(args.dataset, is_test=True)
    elif args.dataset_type == 'open_images':
        dataset = OpenImagesDataset(args.dataset, dataset_type="test")

    true_case_stat, all_gb_boxes, all_difficult_cases = group_annotation_by_class(dataset)
    if args.net == 'vgg16-ssd':
        net = create_vgg_ssd(len(class_names), is_test=True)
    elif args.net == 'mb1-ssd':
        net = create_mobilenetv1_ssd(len(class_names), is_test=True)
    elif args.net == 'mb1-ssd-lite':
        net = create_mobilenetv1_ssd_lite(len(class_names), is_test=True)
    elif args.net == 'sq-ssd-lite':
        net = create_squeezenet_ssd_lite(len(class_names), is_test=True)
    elif args.net == 'mb2-ssd-lite':
        net = create_mobilenetv2_ssd_lite(len(class_names), width_mult=args.mb2_width_mult, is_test=True)
    elif args.net == 'jnet-ssd-lite':
        net = create_imJnet_ssd_lite(len(class_names), width_mult=args.mb2_width_mult, is_test=True)
    else:
        logging.fatal("The net type is wrong. It should be one of vgg16-ssd, mb1-ssd and mb1-ssd-lite.")
        parser.print_help(sys.stderr)
        sys.exit(1)  
    import collections
    timer.start("Load Model")
    # net.load(args.trained_model)
    pretrained_dict = torch.load(args.trained_model)
    # new_state_dict = collections.OrderedDict()
    # for k, v in pretrained_dict.items():
    #     print(k)
    #     name = k[7:]
    #     new_state_dict[name] = v
    net.load_state_dict(pretrained_dict)
    net = net.to(DEVICE)
    print(f'It took {timer.end("Load Model")} seconds to load the model.')
    if args.net == 'vgg16-ssd':
        predictor = create_vgg_ssd_predictor(net, nms_method=args.nms_method, device=DEVICE)
    elif args.net == 'mb1-ssd':
        predictor = create_mobilenetv1_ssd_predictor(net, nms_method=args.nms_method, device=DEVICE)
    elif args.net == 'mb1-ssd-lite':
        predictor = create_mobilenetv1_ssd_lite_predictor(net, nms_method=args.nms_method, device=DEVICE)
    elif args.net == 'sq-ssd-lite':
        predictor = create_squeezenet_ssd_lite_predictor(net,nms_method=args.nms_method, device=DEVICE)
    elif args.net == 'mb2-ssd-lite':
        predictor = create_mobilenetv2_ssd_lite_predictor(net, nms_method=args.nms_method, device=DEVICE)
    elif args.net == 'jnet-ssd-lite':
        predictor = create_imJnet_ssd_lite_predictor(net, nms_method=args.nms_method, device=DEVICE)

    else:
        logging.fatal("The net type is wrong. It should be one of vgg16-ssd, mb1-ssd and mb1-ssd-lite.")
        parser.print_help(sys.stderr)
        sys.exit(1)

    results = []
    for i in range(len(dataset)):
        print("process image", i)
        timer.start("Load Image")
        image, gt_mask = dataset.get_image(i)
        print("Load Image: {:4f} seconds.".format(timer.end("Load Image")))
        timer.start("Predict")
        boxes, labels, probs, masks= predictor.predict(image, gt_mask)
        print("Prediction: {:4f} seconds.".format(timer.end("Predict")))
        indexes = torch.ones(labels.size(0), 1, dtype=torch.float32) * i
        results.append(torch.cat([
            indexes.reshape(-1, 1),
            labels.reshape(-1, 1).float(),
            probs.reshape(-1, 1),
            boxes + 1.0  # matlab's indexes start from 1
        ], dim=1))
        orig_image = dataset.get_ori_image(i)
        image_id, annotation = dataset.get_annotation(i)
        gt_boxes, classes, is_difficult = annotation
        # for i in range(gt_boxes.shape[0]):
        #
        #     box = gt_boxes[i, :]
        #     cv2.rectangle(orig_image, (box[0], box[1]), (box[2], box[3]), (0, 0, 255), 1)

        seg_mask = masks[0]
        seg_mask = torch.squeeze(seg_mask)
        seg_mask = seg_mask.cpu().detach().numpy().astype(np.float32)

        for i in range(boxes.size(0)):
            if probs[i] > 0.4:
                box = boxes[i, :]
                b = random.randint(0, 255)
                g = random.randint(0, 255)
                r = random.randint(0, 255)
                box[0] = min(box[0] + 5, orig_image.shape[1] - 1)
                box[1] = min(box[1] + 5, orig_image.shape[0] - 1)
                box[2] = max(box[2] - 5, box[0])
                box[3] = max(box[3] - 5, box[1])
                # cv2.rectangle(orig_image, (box[0], box[1]), (box[2], box[3]), (b, g, r), 2)
                # label = f"""{voc_dataset.class_names[labels[i]]}: {probs[i]:.2f}"""
                # label = f"{class_names[labels[i]]}: {probs[i]:.2f}"
                # cv2.putText(orig_image, label,
                #             (box[0] + 20, box[1] + 40),
                #             cv2.FONT_HERSHEY_SIMPLEX,
                #             1,  # font scale
                #             (255, 0, 255),
                #             2)  # line type
        # orig_image = cv2.resize(orig_image,(512,512))
        ran_str = ''.join(random.sample(string.ascii_letters + string.digits, 20))
        ocr_cropped_bbox = 'eval_results/bbox/'+ ran_str + ".png"
        ocr_cropped_heatmap = 'eval_results/heatmap/' + ran_str + ".png"
        thresh_mask = (seg_mask * 255).astype(np.uint8)
        thresh_mask[thresh_mask > 127] = 255
        thresh_mask[thresh_mask <= 127] = 0
        thresh_mask = cv2.resize(thresh_mask,(gt_mask.shape[1],gt_mask.shape[0]))

        horizon_mask = cv2.Sobel(thresh_mask, cv2.CV_8UC1, 0, 1, ksize=3)
        vertical_mask = cv2.Sobel(thresh_mask, cv2.CV_8UC1, 1, 0, ksize=3)
        horizon_lines = cv2.HoughLinesP(horizon_mask, 1, np.pi / 180, threshold=40, minLineLength=10, maxLineGap=10)
        vertical_lines = cv2.HoughLinesP(vertical_mask, 1, np.pi / 180, threshold=40, minLineLength=10, maxLineGap=10)
        draw_img = cv2.cvtColor(thresh_mask.copy() * 0,cv2.COLOR_GRAY2BGR)
        horizon_angles = []
        rotate_angle = 0
        if horizon_lines is not None:
            for l in horizon_lines:

                dx = l[0][0] - l[0][2]
                dy = l[0][1] - l[0][3]

                theta = np.arctan2(np.array([dy]), np.array([dx]))
                if theta < 0:
                    theta_tmp = np.pi + theta
                else:
                    theta_tmp = theta
                if (4.5 / 18 * np.pi < theta_tmp < 13.5 / 18 * np.pi):
                    continue
                p1 = np.array([l[0][0], l[0][1]])
                p2 = np.array([l[0][2], l[0][3]])
                angle = theta * 180 / np.pi
                horizon_angles.append(180 + angle if angle < 0 else angle - 180)

                cv2.line(draw_img, (p1[0], p1[1]), (p2[0], p2[1]), (255, 0, 0), 1)
            if len(horizon_angles) > 0:
                rotate_angle = np.array(horizon_angles, dtype=np.float32).mean()

        rotate_image = rotate_bound(orig_image,-rotate_angle)

        if vertical_lines is not None:
            for l in vertical_lines:

                dx = l[0][0] - l[0][2]
                dy = l[0][1] - l[0][3]

                theta = np.arctan2(np.array([dy]), np.array([dx]))
                if theta < 0:
                    theta = np.pi + theta
                if (theta <= 4.5 / 18 * np.pi or 13.5 / 18 * np.pi < theta):
                    continue
                p1 = np.array([l[0][0], l[0][1]])
                p2 = np.array([l[0][2], l[0][3]])

                cv2.line(draw_img, (p1[0], p1[1]), (p2[0], p2[1]), (0, 0, 255), 1)

        boxes, labels, probs, masks = predictor.predict(rotate_image, gt_mask)

        indexes = torch.ones(labels.size(0), 1, dtype=torch.float32) * i
        results.append(torch.cat([
            indexes.reshape(-1, 1),
            labels.reshape(-1, 1).float(),
            probs.reshape(-1, 1),
            boxes + 1.0  # matlab's indexes start from 1
        ], dim=1))

        seg_mask = masks[0]
        seg_mask = torch.squeeze(seg_mask)
        seg_mask = seg_mask.cpu().detach().numpy().astype(np.float32)
        Matrix = reverse_rotate(rotate_image, orig_image.shape, rotate_angle)
        for i in range(boxes.size(0)):
            if probs[i] > 0.4:
                box = boxes[i, :]
                b = random.randint(0, 255)
                g = random.randint(0, 255)
                r = random.randint(0, 255)
                x1 = min(box[0] + 5, orig_image.shape[1] - 1)
                y1 = min(box[1] + 5, orig_image.shape[0] - 1)
                x2 = max(box[2] - 5, box[0])
                y2 = max(box[3] - 5, box[1])
                cv2.rectangle(rotate_image, (x1, y1), (x2, y2), (b, g, r), 2)
                pts = np.array([[x1, y1, 1], [x2, y1, 1], [x2, y2, 1], [x1, y2, 1]])
                # print(pts.T.shape, M.shape)
                dst_ps = np.dot(Matrix, pts.T).T
                for p in range(dst_ps.shape[0]):
                    cv2.circle(orig_image, (int(dst_ps[p,0]), int(dst_ps[p,1])), 2, (b, g, r), 2)

        # seg_mask = cv2.applyColorMap(seg_mask, cv2.COLORMAP_JET)
        # cv2.imwrite(ocr_cropped_bbox, orig_image)
        #
        # cv2.imwrite(ocr_cropped_heatmap, seg_mask)
        cv2.imshow('img',orig_image)
        cv2.imshow('drawn_img', draw_img)
        cv2.imshow('seg_mask', seg_mask)
        cv2.imshow('rotate_image', rotate_image)
        cv2.waitKey(0)
    results = torch.cat(results)
    for class_index, class_name in enumerate(class_names):
        if class_index == 0: continue  # ignore background
        prediction_path = eval_path / f"det_test_{class_name}.txt"
        with open(prediction_path, "w") as f:
            sub = results[results[:, 1] == class_index, :]
            for i in range(sub.size(0)):
                prob_box = sub[i, 2:].numpy()
                image_id = dataset.ids[int(sub[i, 0])]
                if prob_box[0] > 0.45:
                    print(
                        image_id + " " + " ".join([str(v) for v in prob_box]),
                        file=f
                    )
    aps = []
    print("\n\nAverage Precision Per-class:")
    for class_index, class_name in enumerate(class_names):
        if class_index == 0:
            continue
        prediction_path = eval_path / f"det_test_{class_name}.txt"
        ap = compute_average_precision_per_class(
            true_case_stat[class_index],
            all_gb_boxes[class_index],
            all_difficult_cases[class_index],
            prediction_path,
            args.iou_threshold,
            args.use_2007_metric
        )
        aps.append(ap)
        print(f"{class_name}: {ap}")

    print(f"\nAverage Precision Across All Classes:{sum(aps)/len(aps)}")




# -*- coding: utf-8 -*-
import torch
import json
import os
import zipfile
import time
from multiprocessing.dummy import Pool as ThreadPool
import numpy as np

''' Constant Configuration '''
delta1 = 1
mu = 1.7
delta2 = 2.65
gamma = 22.48
scoreThreds = 0.3
matchThreds = 5
areaThres = 0  # 40 * 40.5
alpha = 0.1
#pool = ThreadPool(4)


def pose_nms(bboxes, bbox_scores, pose_preds, pose_scores):
    """
    Parametric Pose NMS algorithm
    bboxes:         bbox locations list (n, 4)
    bbox_scores:    bbox scores list (n,)
    pose_preds:     pose locations list (n, 17, 2)
    pose_scores:    pose scores list    (n, 17, 1)
    """
    global ori_pose_preds, ori_pose_scores, ref_dists

    pose_scores[pose_scores == 0] = 1e-5

    final_result = []

    ori_bboxes = bboxes.clone()
    ori_bbox_scores = bbox_scores.clone()
    ori_pose_preds = pose_preds.clone()
    ori_pose_scores = pose_scores.clone()

    xmax = bboxes[:, 2]
    xmin = bboxes[:, 0]
    ymax = bboxes[:, 3]
    ymin = bboxes[:, 1]

    widths = xmax - xmin
    heights = ymax - ymin
    ref_dists = alpha * np.maximum(widths, heights)

    nsamples = bboxes.shape[0]
    human_scores = pose_scores.mean(dim=1)

    human_ids = np.arange(nsamples)
    # Do pPose-NMS
    pick = []
    merge_ids = []
    while human_scores.shape[0] != 0:
        # Pick the one with highest score
        pick_id = torch.argmax(human_scores)
        pick.append(human_ids[pick_id])
        # num_visPart = torch.sum(pose_scores[pick_id] > 0.2)

        # Get numbers of match keypoints by calling PCK_match
        ref_dist = ref_dists[human_ids[pick_id]]
        simi = get_parametric_distance(pick_id, pose_preds, pose_scores, ref_dist)
        num_match_keypoints = PCK_match(pose_preds[pick_id], pose_preds, ref_dist)

        # Delete humans who have more than matchThreds keypoints overlap and high similarity
        delete_ids = torch.from_numpy(np.arange(human_scores.shape[0]))[
            (simi > gamma) | (num_match_keypoints >= matchThreds)]

        if delete_ids.shape[0] == 0:
            delete_ids = pick_id
        #else:
        #    delete_ids = torch.from_numpy(delete_ids)

        merge_ids.append(human_ids[delete_ids])
        pose_preds = np.delete(pose_preds, delete_ids, axis=0)
        pose_scores = np.delete(pose_scores, delete_ids, axis=0)
        human_ids = np.delete(human_ids, delete_ids)
        human_scores = np.delete(human_scores, delete_ids, axis=0)
        bbox_scores = np.delete(bbox_scores, delete_ids, axis=0)

    assert len(merge_ids) == len(pick)
    bboxs_pick = ori_bboxes[pick]
    preds_pick = ori_pose_preds[pick]
    scores_pick = ori_pose_scores[pick]
    bbox_scores_pick = ori_bbox_scores[pick]
    #final_result = pool.map(filter_result, zip(scores_pick, merge_ids, preds_pick, pick, bbox_scores_pick))
    #final_result = [item for item in final_result if item is not None]

    for j in range(len(pick)):
        ids = np.arange(pose_preds.shape[1])
        max_score = torch.max(scores_pick[j, ids, 0])

        if max_score < scoreThreds:
            continue

        # Merge poses
        merge_id = merge_ids[j]
        merge_pose, merge_score = p_merge_fast(
            preds_pick[j], ori_pose_preds[merge_id], ori_pose_scores[merge_id], ref_dists[pick[j]])

        max_score = torch.max(merge_score[ids])
        if max_score < scoreThreds:
            continue

        xmax = max(merge_pose[:, 0])
        xmin = min(merge_pose[:, 0])
        ymax = max(merge_pose[:, 1])
        ymin = min(merge_pose[:, 1])

        if 1.5 ** 2 * (xmax - xmin) * (ymax - ymin) < areaThres:
            continue

        final_result.append({
            'bbox': bboxs_pick[j],
            'bbox_score': bbox_scores_pick[j],
            'keypoints': merge_pose - 0.3,
            'kp_score': merge_score,
            'proposal_score': torch.mean(merge_score) + bbox_scores_pick[j] + 1.25 * max(merge_score)
        })

    return final_result


def filter_result(args):
    score_pick, merge_id, pred_pick, pick, bbox_score_pick = args
    global ori_pose_preds, ori_pose_scores, ref_dists
    ids = np.arange(17)
    max_score = torch.max(score_pick[ids, 0])

    if max_score < scoreThreds:
        return None

    # Merge poses
    merge_pose, merge_score = p_merge_fast(
        pred_pick, ori_pose_preds[merge_id], ori_pose_scores[merge_id], ref_dists[pick])

    max_score = torch.max(merge_score[ids])
    if max_score < scoreThreds:
        return None

    xmax = max(merge_pose[:, 0])
    xmin = min(merge_pose[:, 0])
    ymax = max(merge_pose[:, 1])
    ymin = min(merge_pose[:, 1])

    if 1.5 ** 2 * (xmax - xmin) * (ymax - ymin) < 40 * 40.5:
        return None

    return {
        'keypoints': merge_pose - 0.3,
        'kp_score': merge_score,
        'proposal_score': torch.mean(merge_score) + bbox_score_pick + 1.25 * max(merge_score)
    }


def p_merge(ref_pose, cluster_preds, cluster_scores, ref_dist):
    """
    Score-weighted pose merging
    INPUT:
        ref_pose:       reference pose          -- [17, 2]
        cluster_preds:  redundant poses         -- [n, 17, 2]
        cluster_scores: redundant poses score   -- [n, 17, 1]
        ref_dist:       reference scale         -- Constant
    OUTPUT:
        final_pose:     merged pose             -- [17, 2]
        final_score:    merged score            -- [17]
    """
    dist = torch.sqrt(torch.sum(
        torch.pow(ref_pose[np.newaxis, :] - cluster_preds, 2),
        dim=2
    ))  # [n, 17]

    kp_num = 17
    ref_dist = min(ref_dist, 15)

    mask = (dist <= ref_dist)
    final_pose = torch.zeros(kp_num, 2)
    final_score = torch.zeros(kp_num)

    if cluster_preds.dim() == 2:
        cluster_preds.unsqueeze_(0)
        cluster_scores.unsqueeze_(0)
    if mask.dim() == 1:
        mask.unsqueeze_(0)

    for i in range(kp_num):
        cluster_joint_scores = cluster_scores[:, i][mask[:, i]]  # [k, 1]
        cluster_joint_location = cluster_preds[:, i, :][mask[:, i].unsqueeze(
            -1).repeat(1, 2)].view((torch.sum(mask[:, i]), -1))

        # Get an normalized score
        normed_scores = cluster_joint_scores / torch.sum(cluster_joint_scores)

        # Merge poses by a weighted sum
        final_pose[i, 0] = torch.dot(cluster_joint_location[:, 0], normed_scores.squeeze(-1))
        final_pose[i, 1] = torch.dot(cluster_joint_location[:, 1], normed_scores.squeeze(-1))

        final_score[i] = torch.dot(cluster_joint_scores.transpose(0, 1).squeeze(0), normed_scores.squeeze(-1))

    return final_pose, final_score


def p_merge_fast(ref_pose, cluster_preds, cluster_scores, ref_dist):
    """
    Score-weighted pose merging
    INPUT:
        ref_pose:       reference pose          -- [17, 2]
        cluster_preds:  redundant poses         -- [n, 17, 2]
        cluster_scores: redundant poses score   -- [n, 17, 1]
        ref_dist:       reference scale         -- Constant
    OUTPUT:
        final_pose:     merged pose             -- [17, 2]
        final_score:    merged score            -- [17]
    """
    dist = torch.sqrt(torch.sum(
        torch.pow(ref_pose[np.newaxis, :] - cluster_preds, 2),
        dim=2
    ))

    kp_num = 17
    ref_dist = min(ref_dist, 15)

    mask = (dist <= ref_dist)
    final_pose = torch.zeros(kp_num, 2)
    final_score = torch.zeros(kp_num)

    if cluster_preds.dim() == 2:
        cluster_preds.unsqueeze_(0)
        cluster_scores.unsqueeze_(0)
    if mask.dim() == 1:
        mask.unsqueeze_(0)

    # Weighted Merge
    masked_scores = cluster_scores.mul(mask.float().unsqueeze(-1))
    normed_scores = masked_scores / torch.sum(masked_scores, dim=0)

    final_pose = torch.mul(cluster_preds, normed_scores.repeat(1, 1, 2)).sum(dim=0)
    final_score = torch.mul(masked_scores, normed_scores).sum(dim=0)
    return final_pose, final_score


def get_parametric_distance(i, all_preds, keypoint_scores, ref_dist):
    pick_preds = all_preds[i]
    pred_scores = keypoint_scores[i]
    dist = torch.sqrt(torch.sum(
        torch.pow(pick_preds[np.newaxis, :] - all_preds, 2),
        dim=2
    ))
    mask = (dist <= 1)

    # Define a keypoints distance
    score_dists = torch.zeros(all_preds.shape[0], all_preds.shape[1])
    keypoint_scores.squeeze_()
    if keypoint_scores.dim() == 1:
        keypoint_scores.unsqueeze_(0)
    if pred_scores.dim() == 1:
        pred_scores.unsqueeze_(1)
    # The predicted scores are repeated up to do broadcast
    pred_scores = pred_scores.repeat(1, all_preds.shape[0]).transpose(0, 1)

    score_dists[mask] = torch.tanh(pred_scores[mask] / delta1) *\
                        torch.tanh(keypoint_scores[mask] / delta1)

    point_dist = torch.exp((-1) * dist / delta2)
    final_dist = torch.sum(score_dists, dim=1) + mu * torch.sum(point_dist, dim=1)

    return final_dist


def PCK_match(pick_pred, all_preds, ref_dist):
    dist = torch.sqrt(torch.sum(
        torch.pow(pick_pred[np.newaxis, :] - all_preds, 2),
        dim=2
    ))
    ref_dist = min(ref_dist, 7)
    num_match_keypoints = torch.sum(
        dist / ref_dist <= 1,
        dim=1
    )

    return num_match_keypoints

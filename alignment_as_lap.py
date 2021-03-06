"""Alignment of tractograms as LAP.

Copyright Emanuele Olivetti, 2018
BSD License, 3 clauses.

"""

from __future__ import print_function

import numpy as np
from nibabel import trackvis
from dissimilarity import compute_dissimilarity, dissimilarity
from kmeans import mini_batch_kmeans, compute_labels, compute_centroids
from sklearn.neighbors import KDTree
from dipy.tracking.distances import bundles_distances_mam
from dipy.tracking.streamlinespeed import length
from linear_assignment import LinearAssignment

try:
    from joblib import Parallel, delayed
    joblib_available = True
except ImportError:
    joblib_available = False


def load_tractogram(T_filename, threshold_short_streamlines=10.0):
    """Load tractogram from TRK file and remove short streamlines with
    length below threshold.
    """
    print("Loading %s" % T_filename)
    T, hdr = trackvis.read(T_filename, as_generator=False)
    T = np.array([s[0] for s in T], dtype=np.object)
    print("%s: %s streamlines" % (T_filename, len(T)))

    # Removing short artifactual streamlines
    print("Removing (presumably artifactual) streamlines shorter than %s" % threshold_short_streamlines)
    T = np.array([s for s in T if length(s) >= threshold_short_streamlines], dtype=np.object)
    print("%s: %s streamlines" % (T_filename, len(T)))
    return T


def clustering(S_dr, k, b=100, t=100):
    """Wrapper of the mini-batch k-means algorithm that combines multiple
    basic functions into a convenient one.
    """
    # Generate k random centers:
    S_centers = S_dr[np.random.permutation(S_dr.shape[0])[:k]]

    # Improve the k centers with mini_batch_kmeans
    S_centers = mini_batch_kmeans(S_dr, S_centers, b=b, t=t)

    # Assign the cluster labels to each streamline. The label is the
    # index of the nearest center.
    S_cluster_labels = compute_labels(S_dr, S_centers)

    # Compute a cluster representive, for each cluster
    S_representatives_idx = compute_centroids(S_dr, S_centers)
    return S_representatives_idx, S_cluster_labels


def LAP(S_A, S_B, verbose=True, parallel=True):
    """
    """
    assert(len(S_B) >= len(S_A))  # required by LAP
    if verbose:
        print("Computing LAP between streamlines.")
        print("Computing the distance matrix between the two sets of streamlines.")

    dm_AB = streamline_distance(S_A, S_B, parallel=parallel)
    corresponding_streamlines = LinearAssignment(dm_AB).solution
    return corresponding_streamlines


def streamline_distance(S_A, S_B=None, parallel=True):
    """Wrapper to decide what streamline distance function to use. The
    function computes the distance matrix between sets of
    streamlines. This implementation provides optimiztions like
    parallelization and avoiding useless computations when S_B is
    None.
    """
    distance_function = bundles_distances_mam
    if parallel:
        return dissimilarity(S_A, S_B, distance_function)
    else:
        return distance_function(S_A, S_B)


def distance_corresponding(A, B, correspondence):
    """Distance between streamlines in set A and the corresponding ones in
    B. The vector 'correspondence' has in position 'i' the index of
    the streamline in B that corresponds to A[i].

    """
    return np.array([streamline_distance([A[i]], [B[correspondence[i]]], parallel=False) for i in range(len(A))]).squeeze()


def LAP_two_clusters(cluster_A, cluster_B, alpha=0.5,
                                max_iter1=100, max_iter2=100,
                                parallel=True):
    """Wrapper of LAP() between the streamlines of two clusters. This code
    is able two handle clusters of different sizes and to invert the
    result of corresponding_streamlines, if necessary.
    """
    if len(cluster_A) <= len(cluster_B):  # LAP(A,B)
        corresponding_streamlines = LAP(cluster_A,
                                        cluster_B,
                                        verbose=False,
                                        parallel=parallel)
    else:  # LAP(B,A)
        corresponding_streamlines = LAP(cluster_B,
                                        cluster_A,
                                        verbose=False,
                                        parallel=parallel)
        # invert result from B->A to A->B:
        tmp = -np.ones(len(cluster_A), dtype=np.int)
        for j, v in enumerate(corresponding_streamlines):
            if v != -1:
                tmp[v] = j

        corresponding_streamlines = tmp

    return corresponding_streamlines


def LAP_all_corresponding_pairs(T_A, T_B, k,
                                T_A_cluster_labels,
                                T_B_cluster_labels,
                                corresponding_clusters):
    """Loop over all pairs of correponding clusters and perform graph
    matching between the streamlines of correponding clusters.

    This code executes parallel (multicore) for-loop, if joblib is
    available. If not, it reverts to a standard for-loop.
    """
    print("Compute LAP between streamlines of corresponding clusters")
    correspondence_lap = -np.ones(len(T_A), dtype=np.int)  # container of the results
    if joblib_available:
        print("Parallel version: executing %s tasks in parallel" % k)
        n_jobs = -1
        clusters_A_idx = [np.where(T_A_cluster_labels == i)[0] for i in range(k)]
        clusters_A = [T_A[clA_idx] for clA_idx in clusters_A_idx]
        clusters_B_idx = [np.where(T_B_cluster_labels == corresponding_clusters[i])[0] for i in range(k)]
        clusters_B = [T_B[clB_idx] for clB_idx in clusters_B_idx]
        results = Parallel(n_jobs=n_jobs, verbose=True)(delayed(LAP_two_clusters)(clusters_A[i], clusters_B[i], parallel=False) for i in range(k))
        # merge results
        for i in range(k):
            tmp = results[i] != -1
            correspondence_lap[clusters_A_idx[i][tmp]] = clusters_B_idx[i][results[i][tmp]]

    else:
        for i in range(k):
            print("LAP between streamlines of corresponding clusters: cl_A=%s <-> cl_B=%s" % (i, corresponding_clusters[i]))
            cluster_A_idx = np.where(T_A_cluster_labels == i)[0]
            cluster_A = T_A[cluster_A_idx]
            cluster_B_idx = np.where(T_B_cluster_labels == corresponding_clusters[i])[0]
            cluster_B = T_B[cluster_B_idx]
            corresponding_streamlines = LAP_two_clusters(cluster_A,
                                                         cluster_B)

            tmp = corresponding_streamlines != -1
            correspondence_lap[cluster_A_idx[tmp]] = cluster_B_idx[corresponding_streamlines[tmp]]

    return correspondence_lap


def fill_missing_correspondences(correspondence_lap, T_A_dr):
    """After LAP, in case some correspondences are missing,
    i.e. their target index is '-1', fill them following this idea:
    for a given streamline T_A[i], its correponding one in T_B is the
    one corresponding to the nearest neighbour of T_A[i] in T_A.

    The (approximate nearest neighbour) is computed with a KDTree on
    the dissimilarity representation of T_A, i.e. T_A_dr.
    """
    print("Filling missing correspondences in T_A with the corresponding to their nearest neighbour in T_A")
    correspondence = correspondence_lap.copy()
    T_A_corresponding_idx = np.where(correspondence != -1)[0]
    T_A_missing_idx = np.where(correspondence == -1)[0]
    T_A_corresponding_kdt = KDTree(T_A_dr[T_A_corresponding_idx])
    T_A_missing_NN = T_A_corresponding_kdt.query(T_A_dr[T_A_missing_idx], k=1, return_distance=False).squeeze()
    correspondence[T_A_missing_idx] = correspondence[T_A_corresponding_idx[T_A_missing_NN]]
    return correspondence


def alignment_as_LAP(T_A_filename, T_B_filename,
                     k, threshold_short_streamlines=10.0,
                     b=100,
                     t=100):
    # 1) load T_A and T_B
    T_A = load_tractogram(T_A_filename, threshold_short_streamlines=threshold_short_streamlines)
    T_B = load_tractogram(T_B_filename, threshold_short_streamlines=threshold_short_streamlines)
    
    # 2) Compute the dissimilarity representation of T_A and T_B
    print("Computing the dissimilarity representation of T_A")
    T_A_dr, prototypes_A = compute_dissimilarity(T_A)
    print("Computing the dissimilarity representation of T_B")
    T_B_dr, prototypes_B = compute_dissimilarity(T_B)

    # 3) Compute the k-means clustering of T_A and T_B
    b = 100  # mini-batch size
    t = 100  # number of iterations
    print("Computing the k-means clustering of T_A and T_B, k=%s" % k)
    print("mini-batch k-means on T_A")
    T_A_representatives_idx, T_A_cluster_labels = clustering(T_A_dr, k=k, b=b, t=t)
    print("mini-batch k-means on T_B")
    T_B_representatives_idx, T_B_cluster_labels = clustering(T_B_dr, k=k, b=b, t=t)

    # 4) Compute LAP between T_A_representatives and T_B_representatives
    alpha = 0.5
    max_iter1 = 100
    max_iter2 = 100
    corresponding_clusters = LAP(T_A[T_A_representatives_idx],
                                 T_B[T_B_representatives_idx])
    distance_clusters = distance_corresponding(T_A[T_A_representatives_idx],
                                               T_B[T_B_representatives_idx],
                                               corresponding_clusters)
    print("Median distance between corresponding clusters: %s" % np.median(distance_clusters))

    # 5) For each pair corresponding cluster, compute LAP
    # between their streamlines
    correspondence_lap = LAP_all_corresponding_pairs(T_A, T_B, k,
                                                     T_A_cluster_labels,
                                                     T_B_cluster_labels,
                                                     corresponding_clusters)

    # 6) Filling the missing correspondences in T_A with the
    # correspondences of the nearest neighbors in T_A
    correspondence = fill_missing_correspondences(correspondence_lap, T_A_dr)

    # 7) Compute the mean distance of corresponding streamlines, to
    # check the quality of the result
    distances = distance_corresponding(T_A, T_B, correspondence)
    print("Median distance of corresponding streamlines: %s" % np.median(distances))

    return correspondence, distances


if __name__ == '__main__':
    print(__doc__)
    np.random.seed(0)

    # T_A_filename = 'data/HCP_subject124422_100Kseeds/tracks_dti_100K.trk'
    # T_B_filename = 'data/HCP_subject124422_100Kseeds/tracks_dti_100K.trk'
    # T_A_filename = 'data2/100307/Tractogram/tractogram_b1k_1.25mm_csd_wm_mask_eudx1M.trk'
    # T_B_filename = 'data2/100408/Tractogram/tractogram_b1k_1.25mm_csd_wm_mask_eudx1M.trk'
    T_A_filename = 'miccai2018_dataset/deterministic_tracking_dipy_FNAL/sub-100307/sub-100307_var-FNAL_tract.trk'
    T_B_filename = 'miccai2018_dataset/deterministic_tracking_dipy_FNAL/sub-109123/sub-109123_var-FNAL_tract.trk'

    # Main parameters:
    k = 1000  # number of clusters, usually somewhat above sqrt(|T_A|) is optimal for efficiency.
    threshold_short_streamlines = 0.0  # Beware: discarding streamlines affects IDs

    # Additional internal parameters, no need to change them, usually:
    b = 100
    t = 100

    correspondence, distances = alignment_as_LAP(T_A_filename, T_B_filename,
                                                 k=k,
                                                 threshold_short_streamlines=threshold_short_streamlines,
                                                 b=b,
                                                 t=t)

    print("Saving the result into correspondence.csv")
    result = np.vstack([range(len(correspondence)), correspondence]).T
    np.savetxt("correspondence.csv", result, fmt='%d', delimiter=',',
               header='ID_A,ID_B')

    import matplotlib.pyplot as plt
    plt.interactive(True)
    plt.figure()
    plt.hist(distances, bins=50)
    plt.title("Distances between corresponding streamlines")

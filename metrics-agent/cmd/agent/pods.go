package main

import (
	"context"
	"fmt"
	"os"

	v1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/fields"
	"k8s.io/client-go/kubernetes"
)

// cgroupFSRoot is the base of the cgroup v2 mount as seen inside the agent
// container (see daemonset.yaml's hostPath mount of /sys/fs/cgroup).
const cgroupFSRoot = "/sys/fs/cgroup"

// localPod is the trimmed-down view of a v1.Pod this agent actually needs.
type localPod struct {
	name        string
	uid         string
	qosClass    v1.PodQOSClass
	annotations map[string]string
}

// listLocalPods lists pods scheduled on nodeName via a field-selector watch on
// the API server (cheap: filtered server-side, not a full-cluster list).
func listLocalPods(ctx context.Context, clientset *kubernetes.Clientset, nodeName string) ([]localPod, error) {
	selector := fields.OneTermEqualSelector("spec.nodeName", nodeName).String()
	podList, err := clientset.CoreV1().Pods("").List(ctx, metav1.ListOptions{
		FieldSelector: selector,
	})
	if err != nil {
		return nil, fmt.Errorf("list pods on node %s: %w", nodeName, err)
	}

	out := make([]localPod, 0, len(podList.Items))
	for _, p := range podList.Items {
		if p.Status.Phase != v1.PodRunning {
			continue
		}
		out = append(out, localPod{
			name:        p.Namespace + "/" + p.Name,
			uid:         string(p.UID),
			qosClass:    p.Status.QOSClass,
			annotations: p.Annotations,
		})
	}
	return out, nil
}

// kubepodsUnitNames returns the systemd unit names (not full paths) for a
// pod's qos-level slice ("" for Guaranteed, which has no qos segment) and its
// own pod-level slice, e.g. "kubepods-burstable.slice" /
// "kubepods-burstable-pod<uid>.slice". Guaranteed pods nest directly under
// kubepods.slice with no qos segment; Burstable/BestEffort pods embed the qos
// class in both the parent slice name and their own slice name.
func kubepodsUnitNames(qos v1.PodQOSClass, uid string) (qosSlice, podSlice string) {
	u := dashUID(uid)
	switch qos {
	case v1.PodQOSBurstable:
		return "kubepods-burstable.slice", fmt.Sprintf("kubepods-burstable-pod%s.slice", u)
	case v1.PodQOSGuaranteed:
		return "", fmt.Sprintf("kubepods-pod%s.slice", u)
	default: // BestEffort
		return "kubepods-besteffort.slice", fmt.Sprintf("kubepods-besteffort-pod%s.slice", u)
	}
}

// resolvePodCgroupPath maps a pod UID to its cgroup v2 directory. The path shape
// depends on the cgroup driver (systemd vs cgroupfs) and QoS class — this
// implements the systemd-driver layout, which is the default for kubelet since
// 1.22+.
//
// It tries two layouts because this varies per stand/cluster (docs §3.1,
// §3.3 — cross-check against the actual kubelet --cgroup-driver config):
//   - classic: kubelet runs directly under system.slice, so kubepods.slice is
//     a top-level unit (/sys/fs/cgroup/kubepods.slice/...).
//   - nested: kubelet itself runs inside its own kubelet.slice (seen on this
//     Docker Desktop/kind-based local cluster) — systemd then nests the whole
//     kubepods hierarchy one level deeper and renames every descendant unit
//     with a "kubelet-" prefix (/sys/fs/cgroup/kubelet.slice/kubelet-kubepods.slice/...).
func resolvePodCgroupPath(p localPod) (string, error) {
	if p.uid == "" {
		return "", fmt.Errorf("pod %s has no UID", p.name)
	}

	qosSlice, podSlice := kubepodsUnitNames(p.qosClass, p.uid)

	classic := cgroupFSRoot + "/kubepods.slice"
	if qosSlice != "" {
		classic += "/" + qosSlice
	}
	classic += "/" + podSlice
	if _, err := os.Stat(classic); err == nil {
		return classic, nil
	}

	nested := cgroupFSRoot + "/kubelet.slice/kubelet-kubepods.slice"
	if qosSlice != "" {
		nested += "/kubelet-" + qosSlice
	}
	nested += "/kubelet-" + podSlice
	if _, err := os.Stat(nested); err == nil {
		return nested, nil
	}

	return "", fmt.Errorf("cgroup not found for pod %s (tried %s and %s)", p.name, classic, nested)
}

// dashUID converts a UUID's dashes to underscores as systemd's unit-name escaping
// expects (e.g. "kubepods-pod1234_5678_....slice").
func dashUID(uid string) string {
	out := make([]byte, len(uid))
	for i := 0; i < len(uid); i++ {
		if uid[i] == '-' {
			out[i] = '_'
		} else {
			out[i] = uid[i]
		}
	}
	return string(out)
}

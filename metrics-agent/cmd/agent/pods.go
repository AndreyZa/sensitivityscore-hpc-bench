package main

import (
	"context"
	"fmt"

	v1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/fields"
	"k8s.io/client-go/kubernetes"
)

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

// resolvePodCgroupPath maps a pod UID to its cgroup v2 directory. The path shape
// depends on the cgroup driver (systemd vs cgroupfs) and QoS class — this
// implements the systemd-driver layout, which is the default for kubelet since
// 1.22+. Cross-check against the actual stand's kubelet config
// (--cgroup-driver) before relying on this in config A/B (docs §3.1, §3.3).
func resolvePodCgroupPath(p localPod) (string, error) {
	if p.uid == "" {
		return "", fmt.Errorf("pod %s has no UID", p.name)
	}

	var qosSegment string
	switch p.qosClass {
	case v1.PodQOSGuaranteed:
		qosSegment = ""
	case v1.PodQOSBurstable:
		qosSegment = "kubepods-burstable.slice/"
	default: // BestEffort
		qosSegment = "kubepods-besteffort.slice/"
	}

	podSlice := fmt.Sprintf("kubepods-pod%s.slice", dashUID(p.uid))
	path := fmt.Sprintf("/sys/fs/cgroup/kubepods.slice/%s%s", qosSegment, podSlice)
	return path, nil
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

// Command scheduler is the entrypoint binary that registers SensitivityScore with
// the Kubernetes Scheduler Framework's out-of-tree plugin mechanism and starts a
// second scheduler process alongside the default kube-scheduler (profile
// "sensitivityscore-scheduler" in k8s/scheduler-config/scheduler-config.yaml).
package main

import (
	"os"

	"k8s.io/kubernetes/cmd/kube-scheduler/app"

	"github.com/andrey-phd/sensitivityscore-hpc-bench/scheduler-plugin/pkg/plugin"
)

func main() {
	// app.NewSchedulerCommand wires up the standard kube-scheduler CLI
	// (--config, --kubeconfig, --leader-elect, ...) and lets us register
	// additional out-of-tree plugins via WithPlugin.
	command := app.NewSchedulerCommand(
		app.WithPlugin(plugin.Name, plugin.New),
	)

	if err := command.Execute(); err != nil {
		os.Exit(1)
	}
}

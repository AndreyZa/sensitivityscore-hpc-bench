package plugin

import (
	"fmt"
	"os"

	"github.com/fsnotify/fsnotify"
	"gopkg.in/yaml.v3"

	"github.com/andrey-phd/sensitivityscore-hpc-bench/scheduler-plugin/pkg/types"
)

// watchWeights loads weights from path immediately, then watches the file (and its
// parent dir, since k8s ConfigMap volume mounts are symlink-swapped on update, not
// edited in place) for changes and calls onChange with the freshly parsed weights.
// This is what makes Глава 3's ablation study (zero out a dimension, re-measure)
// possible without restarting the scheduler pod.
func watchWeights(path string, onChange func(types.Weights)) error {
	w, err := loadWeights(path)
	if err != nil {
		return fmt.Errorf("initial weights load: %w", err)
	}
	onChange(w)

	watcher, err := fsnotify.NewWatcher()
	if err != nil {
		return fmt.Errorf("fsnotify.NewWatcher: %w", err)
	}

	// Watch the directory, not just the file: ConfigMap volume updates replace
	// the symlink target atomically, which fsnotify sees as a CREATE/REMOVE on
	// the directory rather than a WRITE on the file itself.
	dir := dirOf(path)
	if err := watcher.Add(dir); err != nil {
		watcher.Close()
		return fmt.Errorf("watch %s: %w", dir, err)
	}

	go func() {
		defer watcher.Close()
		for {
			select {
			case event, ok := <-watcher.Events:
				if !ok {
					return
				}
				if event.Op&(fsnotify.Write|fsnotify.Create|fsnotify.Rename) == 0 {
					continue
				}
				w, err := loadWeights(path)
				if err != nil {
					// Keep serving the last-known-good weights rather than
					// crashing the scheduler on a transient partial write.
					continue
				}
				onChange(w)
			case _, ok := <-watcher.Errors:
				if !ok {
					return
				}
			}
		}
	}()

	return nil
}

func loadWeights(path string) (types.Weights, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return types.Weights{}, err
	}
	var w types.Weights
	if err := yaml.Unmarshal(data, &w); err != nil {
		return types.Weights{}, err
	}
	return w, nil
}

func dirOf(path string) string {
	for i := len(path) - 1; i >= 0; i-- {
		if path[i] == '/' {
			return path[:i]
		}
	}
	return "."
}

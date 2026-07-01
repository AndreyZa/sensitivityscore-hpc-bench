package plugin

import (
	"github.com/andrey-phd/sensitivityscore-hpc-bench/scheduler-plugin/pkg/metrics"
	"github.com/andrey-phd/sensitivityscore-hpc-bench/scheduler-plugin/pkg/resolver"
)

// newMetricsCacheFromArgs wires up the Redis-backed metrics.Cache from the
// plugin's Args (redisAddr from KubeSchedulerConfiguration pluginConfig).
func newMetricsCacheFromArgs(args *Args) resolver.MetricsReader {
	return metrics.NewCache(args.RedisAddr)
}

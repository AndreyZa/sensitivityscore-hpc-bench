// Package metrics implements the Redis-backed read side of the metrics pipeline
// (docs §3.2): metrics-agent writes node:metrics:<node> / job:metrics:<job>:<node>
// with a TTL, this cache reads node:metrics:<node> for the scheduler's Score().
package metrics

import (
	"context"
	"fmt"
	"strconv"
	"time"

	"github.com/redis/go-redis/v9"

	"github.com/andrey-phd/sensitivityscore-hpc-bench/scheduler-plugin/pkg/types"
)

// Cache wraps a Redis client and implements resolver.MetricsReader.
type Cache struct {
	rdb     *redis.Client
	timeout time.Duration
}

func NewCache(addr string) *Cache {
	return &Cache{
		rdb:     redis.NewClient(&redis.Options{Addr: addr}),
		timeout: 200 * time.Millisecond, // Score() is on the scheduling hot path
	}
}

// GetNodePressure reads node:metrics:<nodeName>, written by metrics-agent
// (HSET ... llc_miss_rate numa_remote_ratio net_bw io_iops ts, EXPIRE 30).
// A missing/expired key is treated as "no pressure data yet" rather than an error,
// so a freshly-joined node with no agent data yet doesn't block scheduling — it's
// surfaced via PressureVector{} (all-zero) plus a non-nil error the caller can log.
func (c *Cache) GetNodePressure(nodeName string) (types.PressureVector, error) {
	ctx, cancel := context.WithTimeout(context.Background(), c.timeout)
	defer cancel()

	key := fmt.Sprintf("node:metrics:%s", nodeName)
	fields, err := c.rdb.HGetAll(ctx, key).Result()
	if err != nil {
		return types.PressureVector{}, fmt.Errorf("redis HGETALL %s: %w", key, err)
	}
	if len(fields) == 0 {
		return types.PressureVector{}, fmt.Errorf("no metrics for node %q yet (stale/missing key %s)", nodeName, key)
	}

	parse := func(k string) float64 {
		v, _ := strconv.ParseFloat(fields[k], 64)
		return v
	}

	return types.PressureVector{
		LLCMissRate:     parse("llc_miss_rate"),
		NUMARemoteRatio: parse("numa_remote_ratio"),
		NetBandwidth:    parse("net_bw"),
		IOPS:            parse("io_iops"),
		Timestamp:       int64(parse("ts")),
	}, nil
}

// GetJobHistory reads the full job:metrics:<jobID>:<nodeName> history written
// across a job's lifetime — used by analysis/, not by the scheduler hot path.
func (c *Cache) GetJobHistory(ctx context.Context, jobID, nodeName string) (map[string]string, error) {
	key := fmt.Sprintf("job:metrics:%s:%s", jobID, nodeName)
	return c.rdb.HGetAll(ctx, key).Result()
}

func (c *Cache) Close() error { return c.rdb.Close() }

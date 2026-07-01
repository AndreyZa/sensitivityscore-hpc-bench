// Package redisclient implements the write side of the metrics pipeline
// (docs §3.2): two key families, node:metrics:<node> (TTL'd, scheduler hot-path
// read) and job:metrics:<job>:<node> (full job-lifetime history, for analysis/).
package redisclient

import (
	"context"
	"fmt"
	"time"

	"github.com/redis/go-redis/v9"
)

type Sample struct {
	LLCMissRate     float64
	NUMARemoteRatio float64
	NetBW           float64
	IOIOPS          float64
	Approximation   string // "" | "host-side", see docs §3.3
}

type Writer struct {
	rdb *redis.Client
	ttl time.Duration
}

func NewWriter(addr string, ttl time.Duration) *Writer {
	return &Writer{
		rdb: redis.NewClient(&redis.Options{Addr: addr}),
		ttl: ttl,
	}
}

// WriteNodeMetrics writes node:metrics:<nodeName> with the configured TTL — this
// is the key the scheduler plugin's metrics.Cache reads for Score() (docs §3.2:
// "EXPIRE node:metrics:<node_name> 30, чтобы не читать протухшие данные
// планировщиком").
func (w *Writer) WriteNodeMetrics(ctx context.Context, nodeName string, s Sample) error {
	key := fmt.Sprintf("node:metrics:%s", nodeName)
	fields := sampleToFields(s)
	if err := w.rdb.HSet(ctx, key, fields).Err(); err != nil {
		return fmt.Errorf("HSET %s: %w", key, err)
	}
	if err := w.rdb.Expire(ctx, key, w.ttl).Err(); err != nil {
		return fmt.Errorf("EXPIRE %s: %w", key, err)
	}
	return nil
}

// WriteJobMetrics writes job:metrics:<jobID>:<nodeName> — written across the
// job's whole lifecycle (not just at scoring time), and deliberately has NO TTL:
// the harness (harness/run_experiment.py) reads this back after job completion to
// build the makespan/variance Parquet dataset (docs §3.2, §5.1), so it must
// survive at least until record_result() has run. The harness is responsible for
// deleting/archiving these keys after export.
func (w *Writer) WriteJobMetrics(ctx context.Context, jobID, nodeName string, s Sample) error {
	key := fmt.Sprintf("job:metrics:%s:%s", jobID, nodeName)
	fields := sampleToFields(s)
	if err := w.rdb.HSet(ctx, key, fields).Err(); err != nil {
		return fmt.Errorf("HSET %s: %w", key, err)
	}
	return nil
}

func sampleToFields(s Sample) map[string]any {
	fields := map[string]any{
		"llc_miss_rate":     s.LLCMissRate,
		"numa_remote_ratio": s.NUMARemoteRatio,
		"net_bw":            s.NetBW,
		"io_iops":           s.IOIOPS,
		"ts":                time.Now().Unix(),
	}
	if s.Approximation != "" {
		fields["approximation"] = s.Approximation
	}
	return fields
}

func (w *Writer) Close() error { return w.rdb.Close() }

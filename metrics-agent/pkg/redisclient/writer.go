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

// WriteJobMetrics accumulates job:metrics:<jobID>:<nodeName> — one HINCRBYFLOAT
// per dimension plus a "samples" counter, so the hash holds running sums over
// the job's whole lifetime and the harness computes lifetime means as
// <dim>_sum / samples. A plain HSET here (the original implementation) meant
// last-write-wins: the harness would read only the final ~5s window before
// completion — usually the teardown phase, not representative of the job.
//
// Deliberately has NO TTL: the harness (harness/run_experiment.py) reads this
// back after job completion to build the Parquet dataset (docs §3.2, §5.1), so
// it must survive at least until record_result() has run. The harness is
// responsible for deleting these keys after export — doubly important now that
// they accumulate: a leftover key from a previous run would silently mix into
// the sums of a rerun with the same job_id.
func (w *Writer) WriteJobMetrics(ctx context.Context, jobID, nodeName string, s Sample) error {
	key := fmt.Sprintf("job:metrics:%s:%s", jobID, nodeName)
	pipe := w.rdb.Pipeline()
	pipe.HIncrByFloat(ctx, key, "llc_miss_rate_sum", s.LLCMissRate)
	pipe.HIncrByFloat(ctx, key, "numa_remote_ratio_sum", s.NUMARemoteRatio)
	pipe.HIncrByFloat(ctx, key, "net_bw_sum", s.NetBW)
	pipe.HIncrByFloat(ctx, key, "io_iops_sum", s.IOIOPS)
	pipe.HIncrBy(ctx, key, "samples", 1)
	pipe.HSet(ctx, key, "ts", time.Now().Unix())
	if s.Approximation != "" {
		pipe.HSet(ctx, key, "approximation", s.Approximation)
	}
	if _, err := pipe.Exec(ctx); err != nil {
		return fmt.Errorf("accumulate %s: %w", key, err)
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

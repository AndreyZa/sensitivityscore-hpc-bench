/*
Copyright 2026 The Kubernetes Authors.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

// Package sensitivityscore implements a Score extension point plugin that
// ranks nodes by interference risk between a job's declared sensitivity
// profile and each node's current measured pressure — the formal task model
// Z = {G, R, S} from the dissertation, where S = (LLC, NUMA, Net, IO) ∈ [0,1]^4.
//
// This is a direct extension of the working MVP (single "noise" scalar +
// JSON-file reload) to the full four-dimensional sensitivity vector. The
// reload mechanism (ticker + os.ReadFile + json.Unmarshal, no fsnotify) is
// kept unchanged on purpose — it's the part that was already proven to build
// and run; only the scoring math and the on-disk schema grew.
package sensitivityscore

import (
	"context"
	"encoding/json"
	"os"
	"sync"
	"time"

	v1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/klog/v2"
	fwk "k8s.io/kube-scheduler/framework"
)

const Name = "SensitivityScore"

// Annotation keys a job uses to declare its own sensitivity profile S_job.
// Values are "high" | "medium" | "low" (mapped to 1.0 / 0.5 / 0.0), matching
// the MVP's boolean annotation but extended from one dimension to four.
const (
	annoLLC  = "scheduling.phd/sensitivity-llc"
	annoNUMA = "scheduling.phd/sensitivity-numa"
	annoNet  = "scheduling.phd/sensitivity-net"
	annoIO   = "scheduling.phd/sensitivity-io"
)

// metricsFilePath - путь к файлу с метриками загрузки нод, монтируется через
// ConfigMap. Формат JSON расширен с одного числа на объект с 4 полями —
// см. schema ниже.
const metricsFilePath = "/etc/sensitivity/node-metrics.json"

// weightsFilePath - путь к файлу с весами измерений S, тоже монтируется через
// ConfigMap, тоже перечитывается тем же тикером. Отдельный файл, а не часть
// node-metrics.json, чтобы менять веса (абляция) не задевая пайплайн метрик.
const weightsFilePath = "/etc/sensitivity/weights.json"

// refreshInterval - как часто перечитывать оба файла.
const refreshInterval = 10 * time.Second

// nodePressure - "давление" на ноде по каждому измерению S, 0-100 на каждой
// оси (100 = "на ноде уже интенсивная нагрузка именно по этому измерению").
// Раньше был один float64 ("noise"); теперь по одному на LLC/NUMA/Net/IO,
// чтобы job с разным профилем чувствительности получали разный score на
// одной и той же ноде.
type nodePressure struct {
	LLC  float64 `json:"llc"`
	NUMA float64 `json:"numa"`
	Net  float64 `json:"net"`
	IO   float64 `json:"io"`
}

// nodeMetrics - формат записи в JSON-файле метрик: имя ноды -> nodePressure.
// Пример файла:
//
//	{
//	  "node-1": {"llc": 20, "numa": 10, "net": 5,  "io": 0},
//	  "node-2": {"llc": 80, "numa": 60, "net": 10, "io": 5}
//	}
type nodeMetrics map[string]nodePressure

// weights - вес каждого измерения в скор-функции, [0, +inf). Хранится
// отдельным файлом именно для того, чтобы абляционные прогоны (Глава 3:
// "что если убрать NUMA из score") делались правкой ConfigMap, а не кода.
// Пример файла: {"llc": 1.0, "numa": 1.0, "net": 1.0, "io": 1.0}
type weights struct {
	LLC  float64 `json:"llc"`
	NUMA float64 `json:"numa"`
	Net  float64 `json:"net"`
	IO   float64 `json:"io"`
}

func defaultWeights() weights {
	return weights{LLC: 1.0, NUMA: 1.0, Net: 1.0, IO: 1.0}
}

// SensitivityScore - плагин с in-memory кэшем метрик и весов, оба
// обновляются периодическим перечитыванием файлов (тот же механизм, что и в
// MVP — без сетевых вызовов на пути Score()).
type SensitivityScore struct {
	handle fwk.Handle

	mu      sync.RWMutex
	metrics nodeMetrics
	weights weights
}

var _ fwk.ScorePlugin = &SensitivityScore{}

func (s *SensitivityScore) Name() string {
	return Name
}

// Score - формализация Z = {G, R, S} из §2.1 плана: score = 100 -
// dot(S_job, Pressure_node) * weight, без сетевых вызовов (кэш уже в памяти).
func (s *SensitivityScore) Score(
	ctx context.Context,
	state fwk.CycleState,
	pod *v1.Pod,
	nodeInfo fwk.NodeInfo,
) (int64, *fwk.Status) {
	nodeName := nodeInfo.Node().Name

	s.mu.RLock()
	pressure := s.metrics[nodeName] // нулевой nodePressure, если данных для ноды ещё нет
	w := s.weights
	s.mu.RUnlock()

	jobProfile := extractSensitivityVector(pod.Annotations)

	// Взвешенное скалярное произведение профиля job и давления ноды по тем
	// же 4 измерениям — чем выше произведение, тем больше риск интерференции.
	interference := jobProfile.llc*pressure.LLC*w.LLC +
		jobProfile.numa*pressure.NUMA*w.NUMA +
		jobProfile.net*pressure.Net*w.Net +
		jobProfile.io*pressure.IO*w.IO

	// Знаменатель — теоретический максимум при всех измерениях = 1.0
	// (сумма весов * 100, т.к. pressure в шкале 0-100). Даёт нормировку в
	// [0, 100] независимо от того, сколько измерений реально "горячие".
	maxPossible := (w.LLC + w.NUMA + w.Net + w.IO) * 100
	var normalized float64
	if maxPossible > 0 {
		normalized = interference / maxPossible // в [0,1] при разумных весах
	}

	score := int64(100 - normalized*100)
	if score < fwk.MinNodeScore {
		score = fwk.MinNodeScore
	}
	if score > fwk.MaxNodeScore {
		score = fwk.MaxNodeScore
	}

	klog.InfoS("SensitivityScore.Score called",
		"pod", pod.Name, "node", nodeName,
		"jobProfile", jobProfile, "pressure", pressure, "score", score)

	return score, nil
}

func (s *SensitivityScore) ScoreExtensions() fwk.ScoreExtensions {
	return nil
}

// sensitivityVector - S_job = (llc, numa, net, io) ∈ [0,1]^4, распарсенный
// из аннотаций пода.
type sensitivityVector struct {
	llc, numa, net, io float64
}

// extractSensitivityVector читает аннотации scheduling.phd/sensitivity-*
// (high|medium|low -> 1.0|0.5|0.0), см. константы annoLLC и т.д. выше.
// Совместимо по духу со старой sensitivityAnnotation ("true"/остальное), но
// на 4 отдельных измерения вместо одного bool.
func extractSensitivityVector(annotations map[string]string) sensitivityVector {
	get := func(key string) float64 {
		switch annotations[key] {
		case "high":
			return 1.0
		case "medium":
			return 0.5
		default: // "low", "", или что угодно нераспознанное — не чувствителен
			return 0.0
		}
	}
	return sensitivityVector{
		llc:  get(annoLLC),
		numa: get(annoNUMA),
		net:  get(annoNet),
		io:   get(annoIO),
	}
}

// New - конструктор. Как и в MVP, аргументы плагина не используются
// (_ runtime.Object) — конфигурация целиком через два ConfigMap-файла, без
// codegen/scheme-регистрации args-типа (см. pkg/podstate для того же
// паттерна "плагин без Args" в этом репозитории).
func New(ctx context.Context, _ runtime.Object, h fwk.Handle) (fwk.Plugin, error) {
	s := &SensitivityScore{
		handle:  h,
		metrics: make(nodeMetrics),
		weights: defaultWeights(),
	}

	go s.refreshLoop(ctx)

	return s, nil
}

// refreshLoop - раз в refreshInterval перечитывает оба файла (метрики и
// веса). Один тикер на оба файла — не нужно два фоновых цикла ради двух
// файлов, которые в любом случае обновляются одним и тем же ConfigMap-mount.
func (s *SensitivityScore) refreshLoop(ctx context.Context) {
	s.reloadMetrics()
	s.reloadWeights()

	ticker := time.NewTicker(refreshInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			s.reloadMetrics()
			s.reloadWeights()
		}
	}
}

func (s *SensitivityScore) reloadMetrics() {
	data, err := os.ReadFile(metricsFilePath)
	if err != nil {
		klog.ErrorS(err, "failed to read sensitivity metrics file", "path", metricsFilePath)
		return
	}

	var m nodeMetrics
	if err := json.Unmarshal(data, &m); err != nil {
		klog.ErrorS(err, "failed to parse sensitivity metrics file")
		return
	}

	s.mu.Lock()
	s.metrics = m
	s.mu.Unlock()
}

// reloadWeights читает веса измерений; при отсутствии/ошибке файла тихо
// остаётся на последних валидных весах (по умолчанию — defaultWeights()),
// а не роняет плагин — веса менее критичны, чем метрики, и отсутствие файла
// на первом старте (до применения ConfigMap) не должно ронять scheduler.
func (s *SensitivityScore) reloadWeights() {
	data, err := os.ReadFile(weightsFilePath)
	if err != nil {
		return // нет файла весов — остаёмся на дефолтных/предыдущих валидных
	}

	var w weights
	if err := json.Unmarshal(data, &w); err != nil {
		klog.ErrorS(err, "failed to parse sensitivity weights file")
		return
	}

	s.mu.Lock()
	s.weights = w
	s.mu.Unlock()
}

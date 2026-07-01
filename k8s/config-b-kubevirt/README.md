# config-b-kubevirt — K8s + KubeVirt

Тот же workload, что в config-a, но внутри `VirtualMachineInstance` — проверка
накладных расходов виртуализации на S-метрики (Программа экспериментов §3, H2).

## Важно перед запуском серии B

1. Образ `geant4-vmdisk` — containerDisk-обёртка над тем же `workload/Dockerfile`
   (см. `cloudInitNoCloud` для параметризации профиля).
2. Перед первым прогоном обязательно выполнить health-check vPMU-доступности
   (см. `metrics-agent/pkg/vpmu/healthcheck.go`) — если vPMU passthrough недоступен
   на стенде, `metrics.phd/approximation: host-side` остаётся актуальным и LLC/NUMA
   метрики читаются по cgroup процесса `qemu-kvm`, а не из гостя.
3. Плагин SensitivityScore должен использовать `QemuProcessResolver`
   (`scheduler-plugin/pkg/resolver/qemu_process_resolver.go`), а не
   `PodCgroupResolver` — иначе `nodeState.Pressure` будет читаться из cgroup
   `virt-launcher`-пода, а не реального процесса нагрузки.

```bash
kubectl apply -f vmi-low-s.yaml
kubectl apply -f vmi-high-s.yaml
```

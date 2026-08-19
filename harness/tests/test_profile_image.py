"""Пер-профильный образ (введён 19.08.2026 под ML-жертву, рекалибровка v2):
spec.image перекрывает общий workload-образ серии, отсутствие image у
профиля оставляет общий. Контракт критичен молча: перепутанный образ
означал бы, что «ML-жертва» считает Geant4 (и наоборот) при внешне
здоровой серии."""

from profiles import PROFILES


def test_ml_profile_carries_own_image():
    assert PROFILES["ml-inference"].image == "andreyza/mlprobe:dev"


def test_geant4_profiles_use_series_image():
    for name, spec in PROFILES.items():
        if name == "ml-inference":
            continue
        assert spec.image is None, f"{name}: неожиданный собственный образ {spec.image}"


def test_submit_resolves_image_like_k8s_submit():
    # Зеркало выражения в k8s_submit.submit_job: spec.image or cfg-образ.
    cfg_image = "andreyza/geant4:11.2"
    assert (PROFILES["ml-inference"].image or cfg_image) == "andreyza/mlprobe:dev"
    assert (PROFILES["high-s"].image or cfg_image) == cfg_image

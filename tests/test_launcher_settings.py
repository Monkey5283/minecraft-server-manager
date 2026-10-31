from mc_manager.launcher.settings import DesktopSettings, load_settings, save_settings


def test_original_bundled_catalog_moves_to_public_domain(tmp_path):
    settings = DesktopSettings(
        library_directory=str(tmp_path / "my-library"),
        catalog_source="https://monkeycraft-launcher.giddymixer.chatgpt.site/catalog.json",
        memory_mb=6144,
        selected_profile="monkeycraft-vanilla",
    )
    save_settings(tmp_path, settings)
    loaded = load_settings(tmp_path)
    assert loaded.catalog_source == "https://launcher.monkeyservers.com/catalog.json"
    assert loaded.model_dump(exclude={"catalog_source"}) == settings.model_dump(exclude={"catalog_source"})


def test_custom_catalog_and_local_export_are_preserved(tmp_path):
    for source in ["https://packs.example.com/catalog.json", str(tmp_path / "catalog.json")]:
        settings = DesktopSettings(library_directory=str(tmp_path / "library"), catalog_source=source)
        save_settings(tmp_path, settings)
        assert load_settings(tmp_path) == settings

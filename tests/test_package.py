import importlib.util
from pathlib import Path
import zipfile
import base64
import json
import hashlib
import pytest
spec=importlib.util.spec_from_file_location('package_release',Path(__file__).parents[1]/'scripts/package_release.py')
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_archive_excludes_secrets_runtime_and_virtualenv(tmp_path):
    (tmp_path/'src').mkdir()
    (tmp_path/'src/main.py').write_text('print("ready")')
    (tmp_path/'VERSION').write_text('test')
    (tmp_path/'.env').write_text('LLM_API_KEY=private')
    (tmp_path/'.runtime').mkdir()
    (tmp_path/'.runtime/private.json').write_text('private upload')
    output=tmp_path/'artifacts/release.zip'
    module.package(tmp_path,output)
    with zipfile.ZipFile(output) as archive:
        assert all('.env' not in name and '.runtime' not in name for name in archive.namelist())
        assert 'gene-disease-evidence-app/src/main.py' in archive.namelist()


def test_secret_or_symlink_in_source_aborts_packaging(tmp_path):
    (tmp_path/'src').mkdir()
    (tmp_path/'src/main.py').write_text('TOKEN="'+'sk-'+'a'*30+'"')
    with pytest.raises(ValueError,match='Possible secret'):
        module.public_files(tmp_path)
    (tmp_path/'src/main.py').write_text('print("ready")')
    (tmp_path/'src/link.py').symlink_to(tmp_path/'src/main.py')
    with pytest.raises(ValueError,match='symlink'):
        module.public_files(tmp_path)


def test_only_named_valid_screenshot_assets_can_enter_public_package(tmp_path):
    folder=tmp_path/'docs/assets';folder.mkdir(parents=True)
    asset=folder/'home.png'
    asset.write_bytes(b'not an image')
    with pytest.raises(ValueError,match='Invalid PNG'):module.public_files(tmp_path)
    asset.write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aZ0cAAAAASUVORK5CYII='))
    assert asset in module.public_files(tmp_path)
    (folder/'private-scan.png').write_bytes(asset.read_bytes())
    assert folder/'private-scan.png' not in module.public_files(tmp_path)


def test_unreviewed_documents_and_old_plans_cannot_reenter_the_release(tmp_path):
    folder=tmp_path/'docs';folder.mkdir()
    (folder/'USER_GUIDE.md').write_text('Current user guide')
    (folder/'PLAN_0_4.md').write_text('Historical local development notes')
    (folder/'VALIDATION_0_11_2.md').write_text('Historical acceptance record')
    (folder/'new-private-note.md').write_text('Unreviewed material')
    assert module.public_files(tmp_path)==[folder/'USER_GUIDE.md']


def test_curated_assets_cannot_follow_a_symlinked_directory(tmp_path):
    folder=tmp_path/'docs';folder.mkdir()
    external=tmp_path/'other-assets';external.mkdir()
    (folder/'assets').symlink_to(external,target_is_directory=True)
    with pytest.raises(ValueError,match='symlink'):
        module.public_files(tmp_path)


def test_upload_batches_restore_the_same_repository_paths_and_hashes(tmp_path):
    (tmp_path/'src').mkdir();(tmp_path/'src/main.py').write_text('print("ready")')
    (tmp_path/'docs').mkdir();(tmp_path/'docs/USER_GUIDE.md').write_text('Guide')
    (tmp_path/'.github/workflows').mkdir(parents=True)
    (tmp_path/'.github/workflows/checks.yml').write_text('name: check')
    (tmp_path/'VERSION').write_text('test')
    output=tmp_path/'artifacts/upload.zip'
    (tmp_path/'README.md').write_text('English introduction; see README.zh-CN.md')
    (tmp_path/'README.zh-CN.md').write_text('中文介绍；另见README.md')
    batches,_=module.github_upload(tmp_path,output)
    assert all(n<=100 for n in batches.values())
    with zipfile.ZipFile(output) as archive:
        files={'/'.join(Path(name).parts[2:]):archive.read(name)
            for name in archive.namelist() if '/0' in name}
        manifest=json.loads(files.pop('RELEASE_MANIFEST.json'))
        assert set(files)==set(manifest['files'])
        assert all(hashlib.sha256(data).hexdigest()==manifest['files'][name] for name,data in files.items())
        assert 'src/main.py' in files and '.github/workflows/checks.yml' in files
        assert 'docs/USER_GUIDE.md' in files and 'VERSION' in files
        assert files['README.md'].decode().startswith('English introduction')
        assert files['README.zh-CN.md'].decode().startswith('中文介绍')


def test_large_application_tree_is_split_without_losing_paths(tmp_path):
    folder=tmp_path/'src';folder.mkdir()
    for n in range(115):(folder/f'module_{n}.py').write_text('value=1')
    (tmp_path/'VERSION').write_text('test')
    output=tmp_path/'upload.zip'
    batches,_=module.github_upload(tmp_path,output)
    assert all(n<=100 for n in batches.values())
    with zipfile.ZipFile(output) as archive:
        files=['/'.join(Path(name).parts[2:]) for name in archive.namelist() if '/0' in name]
        assert len(files)==117 and len(set(files))==117
        assert all(f'src/module_{n}.py' in files for n in range(115))

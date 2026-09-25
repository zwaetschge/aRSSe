"""The Unraid templates match docker-compose.yml (scripts/check-unraid-templates.py)."""

import copy
import importlib.util
import json
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('check_unraid_templates',
                                              ROOT / 'scripts' / 'check-unraid-templates.py')
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)

pytestmark = pytest.mark.skipif(shutil.which('docker') is None,
                                reason='needs the docker CLI (compose config, no daemon)')


@pytest.fixture(scope='module')
def services():
    result = subprocess.run(
        ['docker', 'compose', '--env-file', str(ROOT / '.env.example'), 'config',
         '--format', 'json'], cwd=ROOT, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)['services']


def template(path):
    return ET.parse(ROOT / path).getroot()


def config_of(root, target):
    return next(c for c in root.findall('Config') if c.get('Target') == target)


def test_templates_match_compose(services):
    assert check.check_all(services) == []


def test_new_compose_variable_is_reported(services):
    changed = copy.deepcopy(services)
    changed['intelligence']['environment']['NEW_SETTING'] = ''
    errors = check.check_all(changed)
    assert any('compose sets NEW_SETTING' in e for e in errors)


def test_drift_in_a_template_is_reported(services):
    path = 'unraid/arsse-intelligence.xml'
    root = template(path)
    config_of(root, 'WEB_PASSWORD').set('Mask', 'false')
    config_of(root, 'TZ').set('Default', 'UTC')
    config_of(root, 'TZ').text = 'UTC'
    config_of(root, 'LOG_LEVEL').set('Target', 'LOGLEVEL')
    root.find('Network').text = 'bridge'
    root.find('TemplateURL').text = 'https://github.com/zwaetschge/aRSSe'

    errors = '\n'.join(check.check_template(path, root, 'intelligence', services))
    assert 'WEB_PASSWORD holds a secret' in errors
    assert "TZ: template default 'UTC' differs from 'Europe/Berlin'" in errors
    assert 'LOGLEVEL is not set in compose' in errors
    assert 'compose sets LOG_LEVEL' in errors
    assert "Network 'bridge'" in errors
    assert 'TemplateURL' in errors


def test_miniflux_template_reaches_the_database_by_name(services):
    root = template('unraid/miniflux.xml')
    database = config_of(root, 'DATABASE_URL')
    assert database.get('Mask') == 'true' and database.get('Required') == 'true'
    assert '@arsse-db/' in database.get('Default')
    assert services['db']['container_name'] == 'arsse-db'
    assert root.findtext('Network') == 'arsse'

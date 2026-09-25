#!/usr/bin/env python3
"""
Check the Unraid templates in unraid/ against docker-compose.yml.

Both describe the same containers; this keeps them from drifting apart:

- every template parses and names its image, the network 'arsse' and a raw
  TemplateURL (Unraid fetches updates from there; a GitHub page is HTML)
- its Name is the container_name in compose, so the host names in the
  defaults (arsse-db, arsse-miniflux) resolve in the network 'arsse'
- every variable exists in compose, every compose variable is in the
  template (or listed below with a reason), and defaults match
- variables with passwords or keys are masked
- ports and paths match the compose service

Usage: scripts/check-unraid-templates.py [--compose-json FILE]
    Without --compose-json, runs 'docker compose config --format json'
    with .env.example (the defaults a new installation starts with).
"""

import argparse
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW_BASE = 'https://raw.githubusercontent.com/zwaetschge/aRSSe/HEAD/'
NETWORK = 'arsse'
SECRET = re.compile(r'PASSWORD|API_KEY|SECRET|TOKEN|DATABASE_URL')

# Template file -> compose service
TEMPLATES = {
    'unraid/miniflux.xml': 'miniflux',
    'unraid/arsse-intelligence.xml': 'intelligence',
}

# Compose variables a template leaves out on purpose
COMPOSE_ONLY = {
    'intelligence': {
        'MINIFLUX_API_KEY_FILE': 'Docker secret, needs an extra mount',
        'WEB_PASSWORD_FILE': 'Docker secret, needs an extra mount',
        'MINIFLUX_PORT': 'only for links when BASE_URL is localhost; the template '
                         'asks for MINIFLUX_PUBLIC_URL',
        'CLUSTERING_MIN_PAIR_SIMILARITY': 'tuning, in /app/data/config.yaml',
        'CLUSTERING_NGRAM_MAX': 'tuning, in /app/data/config.yaml',
        'CLUSTERING_TOPIC_THRESHOLD': 'tuning, in /app/data/config.yaml',
        'DEDUP_THRESHOLD': 'tuning, in /app/data/config.yaml',
        'DEDUP_MIN_BODY_TOKENS': 'tuning, in /app/data/config.yaml',
    },
}

# Defaults that differ on purpose: variable -> the template's default
TEMPLATE_DEFAULTS = {
    'miniflux': {
        # Compose names the database service 'db'; the Unraid container is arsse-db
        'DATABASE_URL': 'postgres://miniflux:CHANGE_ME@arsse-db/miniflux?sslmode=disable',
        # Required: the address of the server, no useful default
        'BASE_URL': '',
    },
    'intelligence': {
        'MINIFLUX_URL': 'http://arsse-miniflux:8080',
        'MINIFLUX_PUBLIC_URL': '',
        'PUID': '99',
        'PGID': '100',
    },
}


def compose_config(env_file: Path) -> dict:
    """Resolved docker-compose.yml as JSON."""
    result = subprocess.run(
        ['docker', 'compose', '--env-file', str(env_file), 'config', '--format', 'json'],
        cwd=ROOT, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.exit(f"docker compose config failed:\n{result.stderr}")
    return json.loads(result.stdout)


def _attr(config: ET.Element, name: str) -> str:
    return config.get(name) or ''


def check_template(path: str, root: ET.Element, service_name: str, services: dict) -> list:
    """Return the problems of one template (empty if it matches compose)."""
    errors = []
    service = services.get(service_name)
    if service is None:
        return [f"{path}: compose has no service '{service_name}'"]

    def expect(condition: bool, message: str) -> None:
        if not condition:
            errors.append(f"{path}: {message}")

    if root.tag != 'Container':
        return [f"{path}: root element is <{root.tag}>, not <Container>"]
    name = root.findtext('Name') or ''
    expect(name == service.get('container_name'),
           f"Name '{name}' differs from container_name '{service.get('container_name')}'")
    expect(root.findtext('Repository') == service.get('image'),
           f"Repository '{root.findtext('Repository')}' differs from the compose image "
           f"'{service.get('image')}'")
    expect(root.findtext('Network') == NETWORK,
           f"Network '{root.findtext('Network')}' is not '{NETWORK}' (containers resolve "
           f"each other's names only in a user-defined network)")
    expect(root.findtext('TemplateURL') == RAW_BASE + path,
           f"TemplateURL '{root.findtext('TemplateURL')}' is not {RAW_BASE + path}")
    expect(bool(root.findtext('WebUI')), "WebUI is missing")

    configs = root.findall('Config')
    for config in configs:
        target = _attr(config, 'Target')
        expect((config.text or '') == _attr(config, 'Default'),
               f"{target}: value '{config.text or ''}' differs from Default "
               f"'{_attr(config, 'Default')}'")

    environment = service.get('environment') or {}
    variables = {_attr(c, 'Target'): c for c in configs if _attr(c, 'Type') == 'Variable'}
    compose_only = COMPOSE_ONLY.get(service_name, {})
    defaults = TEMPLATE_DEFAULTS.get(service_name, {})
    for target, config in variables.items():
        default = _attr(config, 'Default')
        if target not in environment:
            errors.append(f"{path}: {target} is not set in compose")
            continue
        if SECRET.search(target):
            expect(_attr(config, 'Mask') == 'true', f"{target} holds a secret: Mask=\"true\"")
            if target not in defaults:
                # .env holds a placeholder or the real secret: never a default
                expect(default == '', f"{target}: a secret needs an empty default")
                continue
        expected = defaults.get(target, environment[target] or '')
        expect(default == expected,
               f"{target}: template default '{default}' differs from '{expected}'")
    for variable in environment:
        expect(variable in variables or variable in compose_only,
               f"compose sets {variable}, the template does not (add it, or list it "
               f"in COMPOSE_ONLY with a reason)")
    for variable in compose_only:
        expect(variable not in variables, f"{variable} is in COMPOSE_ONLY and the template")

    ports = {str(p.get('target')) for p in service.get('ports') or []}
    for config in configs:
        if _attr(config, 'Type') == 'Port':
            expect(_attr(config, 'Target') in ports,
                   f"port {_attr(config, 'Target')} is not published in compose")
    volumes = {v.get('target') for v in service.get('volumes') or []}
    for config in configs:
        if _attr(config, 'Type') == 'Path':
            expect(_attr(config, 'Target') in volumes,
                   f"path {_attr(config, 'Target')} is not a volume in compose")
    return errors


def check_all(services: dict) -> list:
    errors = []
    for path, service_name in TEMPLATES.items():
        try:
            root = ET.parse(ROOT / path).getroot()
        except (OSError, ET.ParseError) as e:
            errors.append(f"{path}: {e}")
            continue
        errors.extend(check_template(path, root, service_name, services))
    listed = {str(p.relative_to(ROOT)) for p in (ROOT / 'unraid').glob('*.xml')}
    for path in sorted(listed - set(TEMPLATES)):
        errors.append(f"{path}: not checked, add it to TEMPLATES")
    return errors


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Check unraid/*.xml against compose')
    parser.add_argument('--compose-json', type=Path,
                        help="output of 'docker compose config --format json'")
    args = parser.parse_args(argv)
    if args.compose_json:
        config = json.loads(args.compose_json.read_text(encoding='utf-8'))
    else:
        # The defaults, not the values of a local .env
        config = compose_config(ROOT / '.env.example')
    errors = check_all(config['services'])
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        return 1
    print(f"{len(TEMPLATES)} Unraid templates match docker-compose.yml")
    return 0


if __name__ == '__main__':
    sys.exit(main())

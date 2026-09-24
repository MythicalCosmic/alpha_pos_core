#!/usr/bin/env python3
"""Read-only source compatibility gate; imports no Django settings or databases.

Exit 1 means a known incompatible registry or divergent migration-name history
was found. Exit 0 is not proof of full wire/schema compatibility: field types,
data migrations, and historical database contents still need integration tests.
"""
import argparse
import ast
import json
from pathlib import Path


def registry(root):
    path = root / 'base/services/sync/config.py'
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    values = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {'MODEL_MAP', 'SYNC_ORDER'}:
                    values[target.id] = ast.literal_eval(node.value)
    return {name: values['MODEL_MAP'][name] for name in values['SYNC_ORDER']}


def migration_names(root):
    names = {}
    for path in sorted(root.glob('*/migrations/[0-9]*.py')):
        app = path.parent.parent.name
        sequence = path.stem.split('_', 1)[0]
        names.setdefault((app, sequence), set()).add(path.stem)
    return names


def compare(cloud, desktop):
    cloud_models, desktop_models = registry(cloud), registry(desktop)
    cloud_migrations, desktop_migrations = migration_names(cloud), migration_names(desktop)
    collisions = []
    for key in sorted(cloud_migrations.keys() & desktop_migrations.keys()):
        if cloud_migrations[key] != desktop_migrations[key]:
            collisions.append({
                'app': key[0], 'sequence': key[1],
                'cloud': sorted(cloud_migrations[key]),
                'desktop': sorted(desktop_migrations[key]),
            })
    result = {
        'cloud': str(cloud), 'desktop': str(desktop),
        'unsupported_cloud_feed_models': sorted(cloud_models.keys() - desktop_models.keys()),
        'unsupported_desktop_push_models': sorted(desktop_models.keys() - cloud_models.keys()),
        'different_model_targets': {
            key: {'cloud': cloud_models[key], 'desktop': desktop_models[key]}
            for key in sorted(cloud_models.keys() & desktop_models.keys())
            if cloud_models[key] != desktop_models[key]
        },
        'migration_name_collisions': collisions,
        'limitations': 'Source registry/name check only; does not validate field schemas or repair databases.',
    }
    result['known_incompatibility'] = any(result[key] for key in (
        'unsupported_cloud_feed_models', 'unsupported_desktop_push_models',
        'different_model_targets', 'migration_name_collisions',
    ))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cloud', type=Path, required=True, help='Cloud shared-core checkout')
    parser.add_argument('--desktop', type=Path, required=True, help='Desktop shared-core checkout')
    args = parser.parse_args()
    try:
        result = compare(args.cloud.resolve(strict=True), args.desktop.resolve(strict=True))
    except (OSError, SyntaxError, ValueError, KeyError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))
    return int(result['known_incompatibility'])


if __name__ == '__main__':
    raise SystemExit(main())

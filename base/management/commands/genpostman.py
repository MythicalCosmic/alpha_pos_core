import ast
import inspect
import json
import re
import uuid as uuid_mod
from django.core.management.base import BaseCommand
from django.urls import URLPattern, URLResolver, get_resolver


SAMPLE_VALUES = {
    'email': 'user@example.com',
    'password': 'password123',
    'current_password': 'oldpassword',
    'new_password': 'newpassword123',
    'session_id': 1,
    'session_key': 'abc123',
    'name': 'Sample Name',
    'first_name': 'John',
    'last_name': 'Doe',
    'phone_number': '+998901234567',
    'description': 'Sample description',
    'detail': 'Extra detail',
    'quantity': 1,
    'price': '10000.00',
    'amount': '50000.00',
    'total_amount': '50000.00',
    'status': 'ACTIVE',
    'order_type': 'HALL',
    'inkass_type': 'CASH',
    'category_id': 1,
    'product_id': 1,
    'order_id': 1,
    'user_id': 1,
    'slug': 'sample-slug',
    'sort_order': 0,
    'colors': ['#e74c3c'],
    'is_active': True,
    'is_paid': False,
    'display_id': 1,
    'notes': 'Sample notes',
}


class Command(BaseCommand):
    help = 'Generate Postman collection JSON from project URL patterns'

    def add_arguments(self, parser):
        parser.add_argument(
            '-o', '--output',
            type=str,
            default='postman_collection.json',
        )
        parser.add_argument(
            '--base-url',
            type=str,
            default='http://localhost:8000',
        )
        parser.add_argument(
            '--name',
            type=str,
            default='Alpha POS',
        )

    def handle(self, *args, **options):
        resolver = get_resolver()
        endpoints = []
        self._collect(resolver, '', endpoints)

        folders = {}
        for ep in endpoints:
            folders.setdefault(ep['folder'], []).append(ep)

        collection = self._build_collection(folders, options['base_url'], options['name'])

        output = options['output']
        with open(output, 'w') as f:
            json.dump(collection, f, indent=2)

        total = sum(len(items) for items in folders.values())
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(f'  Generated: {output}'))
        self.stdout.write(f'  {total} requests across {len(folders)} folders')
        self.stdout.write('')
        for folder_name, items in sorted(folders.items()):
            self.stdout.write(f'    {folder_name}/')
            for item in items:
                method_tag = f'[{item["method"]}]'
                self.stdout.write(f'      {method_tag:<10} {item["name"]}')
        self.stdout.write('')

    def _collect(self, resolver, prefix, endpoints):
        for pattern in resolver.url_patterns:
            if isinstance(pattern, URLResolver):
                new_prefix = prefix + str(pattern.pattern)
                self._collect(pattern, new_prefix, endpoints)
            elif isinstance(pattern, URLPattern):
                path = prefix + str(pattern.pattern)
                view = pattern.callback
                mod = getattr(view, '__module__', '') or ''
                if 'django.' in mod:
                    continue
                metas = self._analyze(view, path)
                endpoints.extend(metas)

    def _analyze(self, view, path):
        original = view
        while hasattr(original, '__wrapped__'):
            original = original.__wrapped__

        func_name = getattr(original, '__name__', '') or getattr(view, '__name__', '')
        module = inspect.getmodule(original)

        methods = ['GET']
        fields_map = {}
        auth_required = False
        rate_info = None

        if module:
            try:
                mod_source = inspect.getsource(module)
                tree = ast.parse(mod_source)

                for node in ast.walk(tree):
                    if isinstance(node, ast.FunctionDef) and node.name == func_name:
                        methods = self._extract_methods(node)
                        auth_required = self._check_auth(node)
                        rate_info = self._extract_rate_limit(node)
                        fields_map = self._extract_fields(node, module)
                        break
            except (TypeError, OSError):
                pass

        folder = self._get_folder(path)
        path_vars = re.findall(r'<\w+:(\w+)>', path)
        clean_path = re.sub(r'<\w+:(\w+)>', r':\1', path)
        clean_path = '/' + clean_path.strip('/')

        results = []
        for method in methods:
            fields = fields_map.get(method, fields_map.get('default', {}))
            name = self._format_name(func_name, method, len(methods) > 1)

            results.append({
                'name': name,
                'path': clean_path,
                'method': method,
                'auth': auth_required,
                'fields': fields,
                'rate_limit': rate_info,
                'folder': folder,
                'path_vars': path_vars,
                'is_login': func_name == 'login' and method == 'POST',
            })

        return results

    def _extract_methods(self, node):
        for deco in node.decorator_list:
            if isinstance(deco, ast.Name):
                if deco.id == 'require_POST':
                    return ['POST']
                if deco.id == 'require_GET':
                    return ['GET']
            elif isinstance(deco, ast.Call):
                name = self._get_decorator_name(deco)
                if name == 'require_http_methods' and deco.args:
                    arg = deco.args[0]
                    if isinstance(arg, ast.List):
                        return [
                            elt.value for elt in arg.elts
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                        ]
        return ['GET']

    _AUTH_DECORATORS = frozenset({
        'login_required', 'admin_required', 'role_required',
        'permission_required', 'waiter_required', 'cashier_required',
    })

    def _check_auth(self, node):
        # The old heuristic looked for `get_session_key` in the AST body —
        # views don't call it directly any more (the decorators do), so
        # every view ended up flagged as unauthenticated. Match against
        # the decorator names instead, which is what the auth model
        # actually uses.
        for deco in node.decorator_list:
            target = deco.func if isinstance(deco, ast.Call) else deco
            name = getattr(target, 'id', None) or getattr(target, 'attr', None)
            if name in self._AUTH_DECORATORS:
                return True
        return False

    def _extract_rate_limit(self, node):
        for deco in node.decorator_list:
            if isinstance(deco, ast.Call):
                name = self._get_decorator_name(deco)
                if name == 'rate_limit' and len(deco.args) >= 3:
                    try:
                        max_req = deco.args[1].value
                        window = deco.args[2].value
                        return f"{max_req} requests / {window}s"
                    except (AttributeError, IndexError):
                        pass
        return None

    def _extract_fields(self, func_node, module):
        fields_map = {}

        for node in ast.walk(func_node):
            if not isinstance(node, ast.Call):
                continue
            name = self._get_call_name(node)
            if not name or not name.endswith('_request') or name == 'validate_request':
                continue

            validator = getattr(module, name, None)
            if not validator:
                continue

            try:
                val_source = inspect.getsource(validator)
                match = re.search(
                    r"validate_request\s*\(\s*request\s*,\s*\[(.*?)\]",
                    val_source,
                    re.DOTALL,
                )
                if match:
                    raw = match.group(1)
                    field_names = re.findall(r"['\"](\w+)['\"]", raw)
                    fields = {f: SAMPLE_VALUES.get(f, f"sample_{f}") for f in field_names}
                    ctx = self._detect_method_context(func_node, node)
                    fields_map[ctx] = fields
            except (TypeError, OSError):
                pass

        return fields_map

    def _detect_method_context(self, func_node, target_node):
        for node in ast.walk(func_node):
            if not isinstance(node, ast.If):
                continue
            method = self._get_method_from_test(node.test)
            if not method:
                continue
            for child in ast.walk(node):
                if child is target_node:
                    return method
        return 'default'

    def _get_method_from_test(self, test):
        if isinstance(test, ast.Compare):
            left = test.left
            if (isinstance(left, ast.Attribute) and
                    left.attr == 'method' and
                    len(test.comparators) == 1):
                comp = test.comparators[0]
                if isinstance(comp, ast.Constant) and isinstance(comp.value, str):
                    return comp.value
        return None

    def _get_decorator_name(self, deco):
        func = deco.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
        return ''

    def _get_call_name(self, node):
        func = node.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
        return ''

    def _get_folder(self, path):
        path = path.strip('/')
        if not path:
            return 'General'
        first_segment = path.split('/')[0]
        if '-' in first_segment:
            return first_segment.split('-')[0].title()
        return first_segment.title()

    def _format_name(self, func_name, method, multi_method):
        name = func_name.replace('_', ' ').title()
        if multi_method:
            prefixes = {
                'GET': 'List',
                'DELETE': 'Delete',
                'PUT': 'Update',
                'PATCH': 'Patch',
                'POST': 'Create',
            }
            prefix = prefixes.get(method, method)
            name = f"{prefix} {name}"
        return name

    def _build_collection(self, folders, base_url, name):
        items = []
        for folder_name, endpoints in sorted(folders.items()):
            folder_items = []
            for ep in endpoints:
                folder_items.append(self._build_request(ep))
            items.append({
                'name': folder_name,
                'item': folder_items,
            })

        return {
            'info': {
                'name': name,
                '_postman_id': str(uuid_mod.uuid4()),
                'schema': 'https://schema.getpostman.com/json/collection/v2.1.0/collection.json',
            },
            'variable': [
                {'key': 'base_url', 'value': base_url, 'type': 'string'},
                {'key': 'token', 'value': '', 'type': 'string'},
            ],
            'item': items,
        }

    def _build_request(self, ep):
        path = ep['path']
        method = ep['method']

        headers = [
            {'key': 'Content-Type', 'value': 'application/json', 'type': 'text'},
        ]

        if ep['auth']:
            headers.append({
                'key': 'Authorization',
                'value': 'Bearer {{token}}',
                'type': 'text',
            })

        path_parts = [p for p in path.strip('/').split('/') if p]

        url = {
            'raw': '{{base_url}}' + path,
            'host': ['{{base_url}}'],
            'path': path_parts,
        }

        if ep['path_vars']:
            url['variable'] = [
                {'key': v, 'value': '1'} for v in ep['path_vars']
            ]

        request = {
            'method': method,
            'header': headers,
            'url': url,
        }

        if ep['fields'] and method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            request['body'] = {
                'mode': 'raw',
                'raw': json.dumps(ep['fields'], indent=2),
                'options': {'raw': {'language': 'json'}},
            }

        desc_parts = []
        if ep['rate_limit']:
            desc_parts.append(f"Rate limit: {ep['rate_limit']}")
        if ep['auth']:
            desc_parts.append("Requires authentication")
        if desc_parts:
            request['description'] = '\n'.join(desc_parts)

        item = {
            'name': ep['name'],
            'request': request,
        }

        if ep.get('is_login'):
            item['event'] = [
                {
                    'listen': 'test',
                    'script': {
                        'type': 'text/javascript',
                        'exec': [
                            'if (pm.response.code === 200) {',
                            '    var json = pm.response.json();',
                            '    if (json.data && json.data.token) {',
                            '        pm.collectionVariables.set("token", json.data.token);',
                            '    }',
                            '}',
                        ],
                    },
                },
            ]

        return item

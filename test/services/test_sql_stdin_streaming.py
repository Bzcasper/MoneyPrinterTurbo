from unittest.mock import patch

from scripts.assemble_typebeat_project import psql_exec


def test_large_sql_streamed_and_never_placed_in_ssh_argv():
    content = 'BEGIN;\nSELECT 1;\n-- ' + 'a' * 200_000 + '\nCOMMIT;'
    with patch('scripts.assemble_typebeat_project.REMOTE_HOST','bobby-nuc'):
        with patch('scripts.assemble_typebeat_project.subprocess.run') as run:
            psql_exec(content)
    cmd = run.call_args.args[0]
    assert cmd[0] == 'ssh'
    assert '-T' in cmd
    assert 'docker exec -i' in cmd[-1]
    assert ' -c ' not in cmd[-1]
    assert 'aaaaaa' not in repr(cmd)
    assert run.call_args.kwargs['input'] == content
    assert run.call_args.kwargs['check'] is True

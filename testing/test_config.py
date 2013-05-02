import pytest

from devpi_server.config import MyArgumentParser, gendeploy, parseoptions


class TestParser:

    def test_addoption(self):
        parser = MyArgumentParser()
        parser.addoption("--hello", type=str)
        args = parser.parse_args(["--hello", "world"])
        assert args.hello == "world"

    def test_addoption_default_added_to_help(self):
        parser = MyArgumentParser()
        opt = parser.addoption("--hello", type=str, help="x", default="world")
        assert "[world]" in opt.help

    def test_addoption_getdefault(self):
        parser = MyArgumentParser()
        opt = parser.addoption("--hello1", type=str, help="x", default="world1")
        assert "[world1]" in opt.help

    def test_addoption_getdefault(self):
        def getter(name):
            return dict(hello="world2")[name]
        parser = MyArgumentParser(defaultget=getter)
        opt = parser.addoption("--hello", default="world", type=str, help="x")
        assert opt.default == "world2"
        assert "[world2]" in opt.help
        opt = parser.addoption("--hello2", default="world", type=str, help="x")
        assert opt.default == "world"
        assert "[world]" in opt.help

    def test_addgroup(self):
        parser = MyArgumentParser()
        group = parser.addgroup("hello")
        opt = group.addoption("--hello", default="world", type=str, help="x")
        assert opt.default == "world"
        assert "[world]" in opt.help

    def test_addsubparser(self):
        parser = MyArgumentParser()
        sub = parser.add_subparsers()
        p = sub.add_parser("hello")
        assert isinstance(p, MyArgumentParser)

def test_gendeploy(tmpdir):
    config = parseoptions(["x", "--port=3200"])
    gendeploy(config, tmpdir)
    assert tmpdir.check()
    sup = tmpdir.join("supervisord.conf").read()
    redis = tmpdir.join("redis-devpi.conf").read()
    nginx = tmpdir.join("nginx-devpi.conf").read()
    assert "port = 3199" in sup
    assert "--port=3200" in sup
    assert "--redisport=3201" in sup
    assert "port 3201" in redis
    assert "port 3201" in redis
    assert "proxy_pass http://localhost:3200" in nginx
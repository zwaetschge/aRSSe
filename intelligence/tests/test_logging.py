import json
import logging

from news_clustering import setup_logging


def test_json_logging_to_file(config, tmp_path):
    config.logging.json_format = True
    config.logging.file = str(tmp_path / 'out.log')
    setup_logging(config)
    try:
        logging.getLogger('arsse-intelligence').info('hello %s', 'world')
        for handler in logging.getLogger().handlers:
            handler.flush()
        record = json.loads((tmp_path / 'out.log').read_text().splitlines()[-1])
        assert record['message'] == 'hello world'
        assert record['levelname'] == 'INFO'
    finally:
        for handler in logging.getLogger().handlers[:]:
            handler.close()
            logging.getLogger().removeHandler(handler)

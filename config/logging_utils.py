import json
import logging


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
            'time': self.formatTime(record, self.datefmt),
        }
        for key, value in getattr(record, '__dict__', {}).items():
            if key.startswith('ctx_'):
                payload[key[4:]] = value
        if record.exc_info:
            payload['exc'] = self.formatException(record.exc_info)
        return json.dumps(payload)

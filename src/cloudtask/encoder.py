import datetime
import decimal
import json
import uuid


class CloudTaskJSONEncoder(json.JSONEncoder):
    """
    Custom JSON Encoder that handles datetimes, decimals, UUIDs, sets,
    and Pydantic models perfectly for Cloud Tasks payload serialization.
    """

    def default(self, obj):
        if isinstance(obj, datetime.datetime):
            r = obj.isoformat()
            if obj.microsecond:
                r = r[:23] + r[26:]
            if r.endswith("+00:00"):
                r = r[:-6] + "Z"
            return r
        elif isinstance(obj, datetime.date):
            return obj.isoformat()
        elif isinstance(obj, datetime.time):
            if obj.utcoffset() is not None:
                raise ValueError("JSON can't represent timezone-aware times.")
            r = obj.isoformat()
            if obj.microsecond:
                r = r[:12]
            return r
        elif isinstance(obj, decimal.Decimal):
            return str(obj)
        elif isinstance(obj, uuid.UUID):
            return str(obj)
        elif isinstance(obj, (set, frozenset)):
            return list(obj)
        elif hasattr(obj, "model_dump"):
            # Paydantic V2 Models Support
            return obj.model_dump(mode="json")
        else:
            return super().default(obj)

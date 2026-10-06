from personal_data_platform.sources.fitbit.models import CapturedSnapshot, FitbitBundle
from personal_data_platform.sources.fitbit.raw import encode_bundle


def encode_snapshot(snapshot):
    return encode_bundle(FitbitBundle("fixture", (CapturedSnapshot(snapshot, ()),)))[0]

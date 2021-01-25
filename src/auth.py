import yaml
import datetime
import dateutil.tz
from decouple import Csv, config, UndefinedValueError
from opentaxii.auth.api import OpenTAXIIAuthAPI
from opentaxii.entities import Account
from stip.common.rest_api_auth import auth_by_api_key


class StipTaxiiServerAuth(OpenTAXIIAuthAPI):
    def __init__(self):
        pass

    def authenticate(self, username, api_key):
        stip_user = auth_by_api_key(username, api_key)
        if stip_user is None:
            return
        return Account(id=stip_user.id, username=stip_user.username)

    def get_account(self, token):
        return token

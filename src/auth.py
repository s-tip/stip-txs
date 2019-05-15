# -*- coding: utf-8 -*-
import yaml
import datetime
import dateutil.tz
from decouple import Csv, config, UndefinedValueError
from opentaxii.auth.api import OpenTAXIIAuthAPI
from opentaxii.entities import Account
import django.contrib.auth

class StipTaxiiServerAuth(OpenTAXIIAuthAPI):
	def __init__(self):
		pass

	def authenticate(self,username,password):
		stip_user = django.contrib.auth.authenticate(username=username,password=password)
		if stip_user is None:
			return
		return Account(id=stip_user.id,username=stip_user.username)

	def get_account(self,token):
		return token

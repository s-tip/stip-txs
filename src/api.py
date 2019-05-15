# -*- coding: utf-8 -*-
import django
import yaml
import datetime
import dateutil.tz
import gridfs
import traceback
import tempfile
import StringIO
import structlog
from pymongo import Connection
from opentaxii.persistence.api import OpenTAXIIPersistenceAPI
from opentaxii.taxii.entities import (
	ServiceEntity,
	CollectionEntity,
	ContentBlockEntity,
	ContentBindingEntity,
	InboxMessageEntity,
)
from stix.core import STIXPackage
from stix.data_marking import MarkingSpecification
from stix.extensions.marking.simple_marking import SimpleMarkingStructure
from stix.extensions.marking.ais import AISMarkingStructure


#django application 起動
django.setup()

from ctirs.core.mongo.documents import TaxiiServers,InformationSources,Vias,Communities
from ctirs.core.mongo.documents_stix import StixFiles
from mongoengine.queryset.visitor import Q

class StipTaxiiServerAPI(OpenTAXIIPersistenceAPI):
	STIP_SNS_USER_NAME_PREFIX = 'User Name: '

	def __init__(self,
		service_yaml_path,
		community_name,
		taxii_publisher,
		black_account_list,
		version_path):
		with open(service_yaml_path,'r') as fp:
  			services = yaml.load(fp)

		try:
			with open(version_path,'r') as fp:
  				version =  fp.readline().strip()
  		except IOError:
  			version = 'No version information.'

  		print '>>>>>: S-TIP TAXII Server Start: ' + str(version)
  			
  		self.community = Communities.objects.get(name=community_name)
  		self.via = Vias.get_via_taxii_publish(taxii_publisher)
  			
		#opentaxii.taxii.entities.ServiceEntityのリスト作成
		self.services = []

		for service in services:
			id = service['id']
			type_ = service['type']
			del service['id']
			del service['type']
			self.services.append(ServiceEntity(
						type_,
						service,
						id=id))

		#opentaxii.taxii.entities.CollectionEntityのリスト作成
		self.collections = []
		#collectionとserviceの連携を記録
		self.service_to_collection = []

		#account の blacklist を作成
		if len(black_account_list) != 0:
			self.black_account_list = black_account_list.split(',')
		else:
			self.black_account_list = []
		#print '>>>black_account_list :' +str(self.black_account_list)

		id = 0
		for taxii_server in TaxiiServers.objects.all():
			name = taxii_server.collection_name
			description = None
			type_ = 'DATA_FEED'
			volume = None
			accept_all_content = True
			supported_content = None
			available =True
			ce = CollectionEntity(
				name,
				str(id),
				description=description,
				type=type_,
				volume=volume,
				accept_all_content=accept_all_content,
				supported_content=supported_content,
				available=available)
			self.collections.append(ce)


			#service_idとcollection_idの関連を検索
			service_ids = ['collection_management','poll','inbox']
			for service_id in service_ids:
				for service in self.services:
					if service.id == service_id:
						d = {}
						d[service_id] = str(id)
						self.service_to_collection.append(d)
			id += 1

	def init_app(self,app):
		#appは<class 'flask.app.Flask'>
		pass

	#discovery/taxii-data時にはcollection_id = Noneでコールされる
	def get_services(self, collection_id=None):
		#print '>>>>get_services enter'
		#print '>>>>collection_id:' + str(collection_id)
		ret = self.services
		#print '>>>>get_services ret:' + str(ret)
		#print '>>>>get_services exit'
		return ret

	#taxii-data時にコールされる
	#push時にもコールされる
	def get_collections(self,service_id):
		#print '>>>>get_collections enter'
		#print '>>>>service_id:' + str(service_id)

		#opentaxii.taxii.entities.CollectionEntityのリストを返却
		s = None
		#sercice_idに該当するServiceEntityを検索する
		for service in self.services:
			if service.id  == service_id:
				s = service
				break
		#print '>>>>service:' + str(s)

		ret = []
		if s is not None:
			#ServiceEntityと関連付けされているCollectionEntityを検索する
			for ref in self.service_to_collection:
				if ref.has_key(service_id) == True:
					for col in self.collections:
						if col.id == ref[service_id]:
							ret.append(col)
		#print '>>>>ret:' + str(ret)
		#print '>>>>get_collections exit'
		return ret

	#poll時にコールされる
	def get_collection(self,collection_name,service_id):
		#print '>>>>get_collection enter'
		#print '>>>>collection_name:' + str(collection_name)
		#print '>>>>service_id:' + str(service_id)
		cols = self.get_collections(service_id)
		col = None
		for c in cols:
			if c.name == collection_name:
				col = c
				break
		#print '>>>>return:' + str(col)
		#print '>>>>get_collection exit'

		#collection entityを返却する
		return col

	#mongo の stix_files コレクションより開始日時と終了日時の間 を find し cursor を返却する
	def get_stix_files_from_mongo(self,collection_name,start_time,end_time):
		#print '>>>get_stix_files_from_mongo:start_time:' + str(start_time)
		#print '>>>get_stix_files_from_mongo:end_time:' + str(end_time)
		#print '>>>get_stix_files_from_mongo:collection_name:' + str(collection_name)

		#TAXII Server 設定取得
		ts = TaxiiServers.objects.get(collection_name=collection_name)

		#条件絞り込み
		QQ = Q(information_source__in=ts.information_sources)
		#for is_ in ts.information_sources:
		#	print '>>>get_stix_files_from_mongo:information_sources:' + str(is_.name)
		#	print '>>>get_stix_files_from_mongo:information_sources:' + str(is_.id)
		if start_time is not None and end_time is not None:
			QQ = QQ & Q(produced__gt=start_time)
			QQ = QQ & Q(produced__lt=end_time)
               	elif start_time is None and end_time is not None:
			QQ = QQ & Q(produced__lt=end_time)
               	elif start_time is not None and end_time is None:
			QQ = QQ & Q(produced__gt=start_time)
		#print '>>>get_stix_files_from_mongo:count:' + str(StixFiles.objects(QQ).count())
		return StixFiles.objects(QQ)

	#poll時にコールされる
	def get_content_blocks_count(self,collection_id=None,start_time=None,end_time=None,bindings=None):
		#print '>>>>get_content_blocks_count enter'
		#print '>>>>collection_id:' + str(collection_id)
		#print '>>>>start_time:' + str(start_time)
		#print '>>>>end_time:' + str(end_time)
		#print '>>>>bindings:' + str(bindings)
		#content block数を返却する
		#print '>>>>get_content_blocks_count exit'
		#print '>>>>>>>:' + str(len(self.get_content_blocks(collection_id=collection_id,start_time=start_time,end_time=end_time)))
		return len(self.get_content_blocks(collection_id=collection_id,start_time=start_time,end_time=end_time))

	#collection_id から collection_name を取得する
	def get_collection_name_from_collection_id(self,collection_id):
		collection_name = None
		for collection in self.collections:
			if collection.id == collection_id:
				return collection.name
		return None

	#poll時にコールされる
	def get_content_blocks(self,collection_id=None,start_time=None,end_time=None,bindings=None,offset=0,limit=None):
		#print '>>>>get_content_blocks enter'
		#print '>>>>collection_id:' + str(collection_id)
		#print '>>>>start_time:' + str(start_time)
		#print '>>>>end_time:' + str(end_time)
		#print '>>>>bindings:' + str(bindings)
		#print '>>>>offset:' + str(offset)
		#print '>>>>limit:' + str(limit)

		collection_name = self.get_collection_name_from_collection_id(collection_id)
		timestamp_label = datetime.datetime.now(dateutil.tz.tzutc())
		message = 'Message from S-TIP'
		content_binding = ContentBindingEntity('urn:stix.mitre.org:xml:1.1.1',None)

		#opentaxii.taxii.entities.ContentBlockEntityのリストを返却する
		cbs = []
		for stix_file in self.get_stix_files_from_mongo(collection_name,start_time,end_time)[offset:limit]:
			content = stix_file.content.read()
			#ここでフィルタリングする
			stix_like_file = StringIO.StringIO(content)
			stix_package = STIXPackage.from_xml(stix_like_file)
			#username によるフィルタリング
			username = self.get_stip_sns_username(stix_package)
			if username in self.black_account_list:
				#username が black list に含まれる場合はskip
				#print '>>> %s is skipped by black list filter:' + str(stix_package.stix_header.title)
				#import sys
				#sys.stdout.flush()
				continue
			stix_like_file.close()
			cb = ContentBlockEntity(
                                        content,
                                        timestamp_label,
                                        content_binding=content_binding,
                                        id=None,
                                        message=message,
                                        inbox_message_id=None)
			cbs.append(cb)
		return cbs

	#stix_package の user_name取得
	def get_stip_sns_username(self,stix_package):
		try:
			for marking in  stix_package.stix_header.handling:
				if isinstance(marking,MarkingSpecification):
					marking_structure = marking.marking_structures[0]
					if isinstance(marking_structure,SimpleMarkingStructure):
						statement = marking_structure.statement
						if statement.startswith(self.STIP_SNS_USER_NAME_PREFIX):
							return statement[len(self.STIP_SNS_USER_NAME_PREFIX):]
		except:
			return None


	#push時にコールされる
	#引数はInboxMessageEntity
	#opentaxii.taxii.entities.InboxMessageEntityを返却する
	#services.yaml の inbox の設定で  destination_colleciton_required で yes/no の設定ができる
	#no の場合はクライアント側で設定されているとエラーになる
	#poll 同様 collection を指定指定(しかも複数)指定の push が可能であるが、
	#それに対する制御を行う場合は api.py の中で実装しなくてはならない
	def create_inbox_message(self,inbox_message_entity):
		#print '>>>>create_inbox_message enter'
		#print '>>>>create_inbox_message:inbox_message_entity:' + str(inbox_message_entity)
		#print '<<<<create_inbox_message exit'
		return inbox_message_entity

	#push時にコールされる
	#引数はContentBlockEntity
	def create_content_block(self,content_block_entity,collection_ids,service_id):
		#print '>>>>create_content_block enter'
		#print '>>>>create_content_block:content_block_entity:' + str(content_block_entity)
		#print '>>>>create_content_block:collection_ids:' + str(collection_ids)
		#print '>>>>create_content_block:service_id:' + str(service_id)
		#print '<<<<create_content_block exit'
		#content_balock_entity.contentに中身が格納される
		#with open('/home/terra/work/libtaxii/server/output/output_BabyTiger.xml','w') as fp:
		#	fp.write(content_block_entity.content)
		#ctirs に registする
		stix_file_path = tempfile.mktemp(suffix='.xml')
		with open(stix_file_path,'wb+') as fp:
			fp.write(content_block_entity.content)
		try:
			from ctirs.core.stix.regist import regist
			regist(stix_file_path,self.community,self.via)
		except Exception as e:
			os.remove(stix_file_path)
			traceback.print_exc()
			raise e
		return content_block_entity

	def create_result_set(self,result_set_entity):
		#print '>>>>create_result_set enter'
		#print '>>>>create_result_set:result_set_entity' + str(result_set_entity)
		return result_set_entity

	def create_subsciption(self,subscription_entity):
		#print '>>>>create_subscription enter'
		#print '>>>>create_subscription:subscription_entity' + str(subscription_entity)
		return

	def update_subscription(self,subscription_entity):
		#print '>>>>update_subscription enter'
		#print '>>>>create_subscription:subscription_entity' + str(subscription_entity)
		return

	def attach_collection_to_services(collection_id, service_ids):
		#print '>>>>attach_collection_to_services enter'
		#print '>>>>attach_collection_to_services:collection_id' + str(collection_id)
		#print '>>>>attach_collection_to_services:service_ids' + str(service_ids)
		return

	def get_subscription(self,subscription_id):
		#print '>>>>get_subscription enter'
		#print '>>>>get_subscription:subscription_id' + str(subscription_id)
		return

	def get_subscriptions(self,service_id):
		#print '>>>>get_subscriptions enter'
		#print '>>>>get_subscriptions:service_id' + str(service_id)
		return

from mongoengine.queryset.visitor import Q
import django

# django application 起動
django.setup()

from ctirs.core.mongo.documents_stix import StixFiles
from ctirs.core.mongo.documents import TaxiiServers, Vias, Communities
import yaml
import datetime
import dateutil.tz
import gridfs
import traceback
import tempfile
import io
import structlog
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


class StipTaxiiServerAPI(OpenTAXIIPersistenceAPI):
    STIP_SNS_USER_NAME_PREFIX = 'User Name: '

    def __init__(self,
                 service_yaml_path,
                 community_name,
                 taxii_publisher,
                 black_account_list,
                 version_path):
        with open(service_yaml_path, 'r', encoding='utf-8') as fp:
            services = yaml.load(fp)

        try:
            with open(version_path, 'r', encoding='utf-8') as fp:
                version = fp.readline().strip()
        except IOError:
            version = 'No version information.'

        print('>>>>>: S-TIP TAXII Server Start: ' + str(version))

        self.community = Communities.objects.get(name=community_name)
        self.via = Vias.get_via_taxii_publish(taxii_publisher)

        # opentaxii.taxii.entities.ServiceEntityのリスト作成
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

        # opentaxii.taxii.entities.CollectionEntityのリスト作成
        self.collections = []
        # collectionとserviceの連携を記録
        self.service_to_collection = []

        # account の blacklist を作成
        if len(black_account_list) != 0:
            self.black_account_list = black_account_list.split(',')
        else:
            self.black_account_list = []

        id = 0
        for taxii_server in TaxiiServers.objects.all():
            name = taxii_server.collection_name
            description = None
            type_ = 'DATA_FEED'
            volume = None
            accept_all_content = True
            supported_content = None
            available = True
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

            # service_idとcollection_idの関連を検索
            service_ids = ['collection_management', 'poll', 'inbox']
            for service_id in service_ids:
                for service in self.services:
                    if service.id == service_id:
                        d = {}
                        d[service_id] = str(id)
                        self.service_to_collection.append(d)
            id += 1

    def init_app(self, app):
        pass

    # discovery/taxii-data時にはcollection_id = Noneでコールされる
    def get_services(self, collection_id=None):
        ret = self.services
        return ret

    # taxii-data時にコールされる
    # push時にもコールされる
    def get_collections(self, service_id):
        # opentaxii.taxii.entities.CollectionEntityのリストを返却
        s = None
        # sercice_idに該当するServiceEntityを検索する
        for service in self.services:
            if service.id == service_id:
                s = service
                break
        ret = []
        if s is not None:
            # ServiceEntityと関連付けされているCollectionEntityを検索する
            for ref in self.service_to_collection:
                if (service_id in ref):
                    for col in self.collections:
                        if col.id == ref[service_id]:
                            ret.append(col)
        return ret

    # poll時にコールされる
    def get_collection(self, collection_name, service_id):
        cols = self.get_collections(service_id)
        col = None
        for c in cols:
            if c.name == collection_name:
                col = c
                break
        # collection entityを返却する
        return col

    # mongo の stix_files コレクションより開始日時と終了日時の間 を find し cursor を返却する
    def get_stix_files_from_mongo(self, collection_name, start_time, end_time):
        # TAXII Server 設定取得
        ts = TaxiiServers.objects.get(collection_name=collection_name)

        # 条件絞り込み
        QQ = Q(input_community__in=ts.communities)
        QQ = QQ & Q (version__startswith='1.')
        if start_time is not None and end_time is not None:
            QQ = QQ & Q(produced__gt=start_time)
            QQ = QQ & Q(produced__lt=end_time)
        elif start_time is None and end_time is not None:
            QQ = QQ & Q(produced__lt=end_time)
        elif start_time is not None and end_time is None:
            QQ = QQ & Q(produced__gt=start_time)
        return StixFiles.objects(QQ)

    # poll時にコールされる
    def get_content_blocks_count(self, collection_id=None, start_time=None, end_time=None, bindings=None):
        return len(self.get_content_blocks(collection_id=collection_id, start_time=start_time, end_time=end_time))

    # collection_id から collection_name を取得する
    def get_collection_name_from_collection_id(self, collection_id):
        collection_name = None
        for collection in self.collections:
            if collection.id == collection_id:
                return collection.name
        return None

    # poll時にコールされる
    def get_content_blocks(self, collection_id=None, start_time=None, end_time=None, bindings=None, offset=0, limit=None):
        collection_name = self.get_collection_name_from_collection_id(collection_id)
        timestamp_label = datetime.datetime.now(dateutil.tz.tzutc())
        message = 'Message from S-TIP'
        content_binding = ContentBindingEntity('urn:stix.mitre.org:xml:1.1.1', None)

        # opentaxii.taxii.entities.ContentBlockEntityのリストを返却する
        cbs = []
        for stix_file in self.get_stix_files_from_mongo(collection_name, start_time, end_time)[offset:limit]:
            content = stix_file.content.read()
            # ここでフィルタリングする
            stix_like_file = io.StringIO(content.decode('utf-8'))
            stix_package = STIXPackage.from_xml(stix_like_file)
            # username によるフィルタリング
            username = self.get_stip_sns_username(stix_package)
            if username in self.black_account_list:
                # username が black list に含まれる場合はskip
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

    # stix_package の user_name取得
    def get_stip_sns_username(self, stix_package):
        try:
            for marking in stix_package.stix_header.handling:
                if isinstance(marking, MarkingSpecification):
                    marking_structure = marking.marking_structures[0]
                    if isinstance(marking_structure, SimpleMarkingStructure):
                        statement = marking_structure.statement
                        if statement.startswith(self.STIP_SNS_USER_NAME_PREFIX):
                            return statement[len(self.STIP_SNS_USER_NAME_PREFIX):]
        except BaseException:
            return None

    # push時にコールされる
    # 引数はInboxMessageEntity
    # opentaxii.taxii.entities.InboxMessageEntityを返却する
    # services.yaml の inbox の設定で  destination_colleciton_required で yes/no の設定ができる
    # no の場合はクライアント側で設定されているとエラーになる
    # poll 同様 collection を指定指定(しかも複数)指定の push が可能であるが、
    # それに対する制御を行う場合は api.py の中で実装しなくてはならない

    def create_inbox_message(self, inbox_message_entity):
        return inbox_message_entity

    # push時にコールされる
    # 引数はContentBlockEntity
    def create_content_block(self, content_block_entity, collection_ids, service_id):
        # content_balock_entity.contentに中身が格納される
        # with open('/home/terra/work/libtaxii/server/output/output_BabyTiger.xml','w', encoding='utf-8') as fp:
        #     fp.write(content_block_entity.content)
        # ctirs に registする
        stix_file_path = tempfile.mktemp(suffix='.xml')
        with open(stix_file_path, 'wb+') as fp:
            fp.write(content_block_entity.content)
        try:
            from ctirs.core.stix.regist import regist
            regist(stix_file_path, self.community, self.via)
        except Exception as e:
            os.remove(stix_file_path)
            traceback.print_exc()
            raise e
        return content_block_entity

    def create_result_set(self, result_set_entity):
        return result_set_entity

    def create_subsciption(self, subscription_entity):
        return

    def update_subscription(self, subscription_entity):
        return

    def attach_collection_to_services(collection_id, service_ids):
        return

    def get_subscription(self, subscription_id):
        return

    def get_subscriptions(self, service_id):
        return

from .utils import commentall_index, mycomment_index, mycommentall_index
from django.shortcuts import render, get_object_or_404, redirect
from django.http import HttpResponse, HttpResponseRedirect
from datetime import datetime, date, timezone, timedelta
from dateutil.relativedelta import relativedelta
from concurrent import futures
from django.db import models
from django.db.models import Q
import time
from django.views.generic import View, ListView, TemplateView
from django.views.generic.edit import ModelFormMixin
from django.urls import reverse_lazy, reverse
from django.contrib.auth.decorators import login_required
from django.http.response import JsonResponse
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
import re

import json
import unicodedata
from ipware import get_client_ip
import emoji
from urllib.parse import quote, unquote

import elasticsearch

from django.db.models import Count, Sum, Max, Min
import logging
from django.core.exceptions import ObjectDoesNotExist

from comment.models import Channel, Video, Comment, Search as SearchKey, SearchHistory, LiverName, ChannelGroup

from comment.forms import ChannelForm, SearchFormES, SearchFormMYES, SearchCommentForm
import comment.youtube as yt

from comment.utils import check_video, converttimestampText

from vtuber.settings import ES_HOST

# 非同期実行用
executor = futures.ThreadPoolExecutor(max_workers=2)

logger = logging.getLogger(__name__)

PAGE_PER_ITEM = 10
# es_flag = True


MAINTENANCE = False


class Empty:
    pass


def maintenance(request):

    html = 'ただいまメンテナンス中です。<br>終了予定時刻05:00。<br>※問い合わせは<a href="https://twitter.com/comment2434">＠comment2434</a>まで。<br><span style="min-width: 400px; display: inline-block; min-height: 305px;"><a class="twitter-timeline"  width="400" height="300" href="https://twitter.com/comment2434?ref_src=twsrc%5Etfw">Tweets by comment2434</a><script async src="https://platform.twitter.com/widgets.js" charset="utf-8"></script></span>'
    return HttpResponse(html)


def redirectview(request):
    return redirect('/comment')


class coment_list_es_redirect(View):
    def get(self, request, *args, **kwargs):
        redirect_url = reverse('comment:comment_list_es', kwargs=self.kwargs)
        parameters = request.GET.urlencode()
        url = f'{redirect_url}?{parameters}'
        return redirect(url)


class CommentListES(ListView, ModelFormMixin):
    model = Video
    form_class = SearchCommentForm
    success_url = reverse_lazy('comment:comment_list')
    template_name = 'comment/comment_list.html'
    paginate_by = 100

    def get(self, request, *args, **kwargs):
        if MAINTENANCE:
            return redirect(reverse('comment:maintenance'))
        else:
            self.initialize_variables()
            self.set_keyword_and_initial_delay()
            self.set_object_and_url()
            if 'my_flag' not in kwargs:
                if not self.is_video_valid():
                    if self.object.enable == False:
                        logger.info("コメント取得不可対象：" + self.object.title)
                        message = "該当動画はコメント取得不可となった可能性があります。トップページからやり直してください。"
                    else:
                        logger.info("非公開対象：" + self.object.title)
                        message = "該当動画が検索対象外になった可能性があります。トップページからやり直してください。"
                    return self.render_error_message(message)
            else:
                if not self.object.enable or not self.object.public:
                    self.object.title = "★" + self.object.title
            self.set_liver_list_and_keyword()
            if self.keyword:
                self.fetch_and_process_comments()
            return self.render_response()

    def initialize_variables(self):
        self.object = None
        self.object_list = []
        self.liverNameId = None
        self.liver_list = None
        self.keyword = None
        self.initial_delay = None
        self.video_id = None
        self.index = None

    def set_keyword_and_initial_delay(self):
        self.keyword = self.request.GET.get('keyword')
        self.form_class.base_fields['keyword'].initial = self.keyword
        self.video_id = self.kwargs.get('pk', None)
        initial_delay = self.request.GET.get('initial_delay')
        self.initial_delay = initial_delay if initial_delay else -10

    def set_object_and_url(self):
        self.object = Video.objects.get(id=self.video_id)
        self.object.title = emoji.emojize(self.object.title)
        self.object.url = "https://www.youtube.com/embed/" + self.object.id

    def is_video_valid(self):
        # ビデオが有効かどうかをチェックする
        check_result = check_video(self.object)

        # ビデオが有効でない、またはタイトルに特定の文字列が含まれている場合、ビデオは無効とします
        if not self.object.enable or not check_result or "メン限" in self.object.title or "メンバーシップ限定" in self.object.title:
            return False

        return True

    def render_error_message(self, message):
        return render(self.request, self.template_name,
                      {'form': self.get_form(), 'object': self.object, 'object_list': self.object_list,
                       'message': message, 'liver_list': self.liver_list, 'liverNameId': self.liverNameId})

    def set_liver_list_and_keyword(self):
        self.liverNameId = self.request.GET.get('liverNameId')
        if self.liverNameId:
            self.liverNameId = int(self.liverNameId)
            self.liver_list = LiverName.objects.filter(
                id__gt=121).values_list('id', 'liver_name')
            try:
                self.keyword = livername_get_keyword(self.liverNameId)
            except ObjectDoesNotExist as e:
                logger.info("ライバー名ID検出不可")
                message = "ライバー名IDが変更になった可能性があります。トップページからやり直してください。"
                return self.render_error_message(message)

    def fetch_and_process_comments(self):
        if not (check_keyword(self.keyword)):
            message = "不正な検索ワードです。"
            return render(self.request, self.template_name, {'form': self.get_form(), 'message': message})

        logger.info("検索ES(" + self.object.id + "):" +
                    emoji.demojize(self.keyword))

        nowtime = datetime.now(timezone(timedelta(hours=0)))
        target_time = self.object.publishedAt - timedelta(hours=9)
        if self.object.channel.group.no == 0:
            self.index = mycomment_index
        else:
            self.index = commentall_index

        # Elasticsearchからコメントを取得するための基本クエリを作成
        query = make_base_query(self.keyword, self.liverNameId)

        # クエリにビデオIDの条件を追加
        query["query"]["bool"]["must"].append(
            {
                "match_phrase": {
                    "video_id": {
                        "query": self.object.id
                    }
                }
            }
        )

        # クエリにソートと取得数の条件を追加
        query['sort'] = [{"date": {"order": "asc"}}]
        query['size'] = 1000  # コメント取得単位

        MAX_TRY = 100  # 最大試行回数

        from_num = None
        self.object_list = []
        count = 0
        for i in range(1, MAX_TRY):
            if from_num:
                query['from'] = from_num

            # Elasticsearchからコメントを取得
            es = elasticsearch.Elasticsearch(
                "http://" + ES_HOST + ":9200", timeout=60)
            # logger.info("query" + json.dumps(query))
            result = es.search(index=self.index, body=query)
            hits = result["hits"]["hits"]
            count = result["hits"]["total"]["value"]

            # 各コメントを処理
            for hit in hits:
                dict = hit['_source']
                obj = Empty()
                obj.message = dict['message']
                obj.timestampText = dict['timestamptext']
                timestamp = converttimestampText(
                    obj.timestampText) + self.initial_delay
                if timestamp < 0:
                    timestamp = 0
                obj.link = "https://youtu.be/" + \
                    self.object.id + "?t=" + str(timestamp)
                obj.date = dict['date']
                self.object_list.append(obj)

            from_num = len(self.object_list)
            if from_num >= count:
                break

        self.object.count = len(self.object_list)

    def render_response(self):
        form = self.get_form()
        return render(self.request, self.template_name, {'form': form, 'object': self.object, 'object_list': self.object_list, 'liver_list': self.liver_list, 'liverNameId': self.liverNameId, 'keyword': self.keyword})

    def get_queryset(self):
        return Video.objects.none()


class CommentListMY(CommentListES):
    # @login_required
    def get(self, request, *args, **kwargs):
        kwargs['my_flag'] = True
        response = super().get(request, *args, **kwargs)
        return response


def video_list_es_redirect(request):
    redirect_url = reverse('comment:video_list_es')
    parameters = request.GET.urlencode()
    url = f'{redirect_url}?{parameters}'
    return redirect(url)


class VideoListES(ListView, ModelFormMixin):
    model = Video
    form_class = SearchFormES
    success_url = reverse_lazy('comment:video_list_es')
    template_name = 'comment/video_list_es.html'
    paginate_by = 15

    def get(self, request, *args, **kwargs):
        if MAINTENANCE:
            return redirect(reverse('comment:maintenance'))
        else:
            self.object = None
            page_obj = None
            result_summary = None
            self.form_class.base_fields['keyword'].initial = None
            self.form_class.base_fields['search_datatime_start'].initial = ''
            self.form_class.base_fields['search_datatime_end'].initial = ''

            channel_choice = []
            my_flag = True
            if 'my_flag' in kwargs:
                group_list = ChannelGroup.objects.all().values_list(
                    'id', 'groupName').order_by('no')
            else:
                group_list = ChannelGroup.objects.filter(
                    no__gt=0).values_list('id', 'groupName').order_by('no')

                # TODO サービス利用停止
                # if 'only_flag' not in kwargs:
                #    my_flag = False
                #    message = ""
                #    return render(self.request, self.template_name,
                #                  {'form': self.get_form(), 'message': message})

            channel_choice.append(
                ('グループ', tuple(list(group_list)))
            )

            channel_db_list = Channel.objects.filter(enable=True).values_list(
                'id', 'channelName', 'group_id').order_by('no')
            for gr in group_list:
                tmp_gr = []
                for ch in channel_db_list:
                    if gr[0] == ch[2]:
                        tmp_gr.append(ch[0:2])
                channel_choice.append((gr[1], tuple(tmp_gr))),

            self.form_class.base_fields['channelName'].choices = tuple(
                channel_choice)
            self.form_class.base_fields['channelName'].initial = []
            self.form_class.base_fields['ex_channelName'].choices = tuple(
                channel_choice)
            self.form_class.base_fields['ex_channelName'].initial = []

            log_message = ""
            keyword = None
            self.object_list = []
            # 古いidのまま検索してくるアクセスがあるため、DB上には残っている。
            liver_list = LiverName.objects.filter(
                id__gt=121).values_list('id', 'liver_name').order_by('no')

            self.form_class.base_fields['liverNameId'].choices = tuple(
                liver_list)
            self.form_class.base_fields['liverNameId'].initial = []

            liverNameId = self.request.GET.get('liverNameId')
            type = self.request.GET.get('type')
            if not (type):
                if liverNameId:
                    type = '1'
                else:
                    type = '0'
            self.form_class.base_fields['type'].initial = type

            nowtime = datetime.now(timezone(timedelta(hours=9)))

            if type == '1':
                self.form_class.base_fields['liverNameId'].initial = liverNameId
                try:
                    liverNameId = int(liverNameId)
                    keyword = livername_get_keyword(liverNameId)
                except ObjectDoesNotExist as e:
                    logger.info("ライバー名ID検出不可")
                    message = "ライバー名IDが変更になった可能性があります。トップページからやり直してください。"
                    return render(self.request, self.template_name, {'form': self.get_form(), 'message': message})

            else:
                keyword = self.request.GET.get('keyword')
                self.form_class.base_fields['liverNameId'].initial = []
                liverNameId = None
                if not (keyword):
                    # 初期画面
                    # 人気検索キーワード

                    history_rank_list = None
                    history_list = None
                    new_list = None

                    history_flag = self.request.GET.get('history')

                    if (history_flag and history_flag == "True"):
                        history_sub_list = SearchHistory.objects.filter(searchAt__gte=str(
                            nowtime - timedelta(days=1))).distinct().values_list('keyword', 'liverNameId_id', 'clientIP')
                        history_rank_list_dict = {}
                        history_rank_list_Id_flag_dict = {}

                        for history in history_sub_list:
                            keyword, liverNameId_id, clientIP = history
                            word = None
                            if liverNameId_id:
                                word = "b" + str(liverNameId_id)
                            elif keyword:
                                word = "a" + keyword

                            if word:
                                if word in history_rank_list_dict:
                                    history_rank_list_dict[word] += 1
                                else:
                                    history_rank_list_dict[word] = 1

                        history_rank_list = []
                        rank = 1
                        for tmp in sorted(history_rank_list_dict.items(), key=lambda i: i[1], reverse=True):
                            # keywordとliverId型の分離
                            obj = Empty()
                            obj.rank = rank
                            if tmp[0][0] == 'b':

                                # debug
                                if False:
                                    obj.type = "ライバー名検索"
                                    # obj.url = reverse('comment:video_list_es', kwargs=self.kwargs)
                                    obj.url = reverse(
                                        'comment:video_list_es') + "?liverNameId=" + tmp[0][1:]
                                    obj.word = livername_get_liver_name(
                                        int(tmp[0][1:]))
                                continue
                            else:
                                obj.type = "キーワード検索"
                                obj.url = "?keyword=" + \
                                    emoji.emojize(tmp[0][1:])
                                obj.word = tmp[0][1:]
                            obj.count = tmp[1]
                            history_rank_list.append(obj)
                            rank += 1
                            if rank > 50:
                                break

                    # 検索履歴
                    if 'my_flag' in kwargs:
                        history_sub_list = SearchHistory.objects.filter(searchAt__gte=str(
                            nowtime - timedelta(days=1))).values_list('keyword', 'liverNameId_id', 'clientIP', 'searchAt').order_by('-searchAt')

                        history_list = []
                        pre_word = ""
                        pre_clientIP = ""
                        for history in history_sub_list:
                            keyword, liverNameId_id, clientIP, searchAt = history

                            # keywordとliverId型の分離
                            obj = Empty()
                            obj.searchAt = searchAt
                            if liverNameId_id:
                                # debug
                                if False:
                                    obj.type = "ライバー名検索"
                                    # obj.url = reverse('comment:video_list_es', kwargs=self.kwargs)
                                    obj.url = reverse(
                                        'comment:video_list_es') + "?liverNameId=" + tmp[0][1:]
                                    obj.word = livername_get_liver_name(
                                        int(keyword))
                                continue
                            elif keyword:
                                obj.type = "キーワード検索"
                                obj.url = "?keyword=" + emoji.emojize(keyword)
                                obj.word = keyword

                            if not (pre_word == obj.word and pre_clientIP == clientIP):
                                history_list.append(obj)
                                pre_word = obj.word
                                pre_clientIP = clientIP

                    # 新着動画
                    if False:
                        new_list = []
                        nulltime = datetime.strptime(
                            '1970-01-01 00:00:00', '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone(timedelta(hours=0)))
                        new_db_list = Video.objects.filter(collecting_flag=0, enable=1, public=1).order_by(
                            '-collectedAt', '-publishedAt')[:100]

                        page_obj = paginate_query(
                            request, new_db_list, PAGE_PER_ITEM)
                        for video in page_obj:
                            obj = Empty()
                            obj.title = emoji.emojize(video.title)
                            obj.img = "https://i3.ytimg.com/vi/" + video.id + "/mqdefault.jpg"
                            if 'my_flag' in kwargs:
                                obj.url = reverse(
                                    'comment:comment_list_my', kwargs=dict(pk=video.id))
                            else:
                                obj.url = reverse(
                                    'comment:comment_list_es', kwargs=dict(pk=video.id))
                            obj.publishedAt = video.publishedAt
                            obj.comment_count = video.comment_count
                            new_list.append(obj)

                    self.object_list = Video.objects.none()
                    self.form_class.base_fields['mode'].initial = '0'
                    response = render(self.request, self.template_name,
                                      {'form': self.get_form(), 'object_list': self.object_list, 'page_obj': page_obj,
                                       'result_summary': result_summary, 'liver_list': liver_list, 'history_rank_list': history_rank_list, 'history_list': history_list, 'new_list': new_list, 'my_flag': my_flag})

                    return response

                self.form_class.base_fields['keyword'].initial = keyword

                if not (re.search(r'[^ 　]', keyword)):
                    message = "検索ワードを入力してください。"
                    return render(self.request, self.template_name, {'form': self.get_form(), 'message': message})

                if not (check_keyword(keyword)):
                    message = "不正な検索ワードです。"
                    return render(self.request, self.template_name, {'form': self.get_form(), 'message': message})

            log_message = log_message+"検索:" + keyword

            channelList = self.request.GET.getlist('channelName')

            if channelList and len(channelList) > 0:
                log_message = log_message + ",channel=" + str(channelList)
                # チャンネル選択初期値設定
                self.form_class.base_fields['channelName'].initial = channelList

            ex_channelList = self.request.GET.getlist('ex_channelName')

            if ex_channelList and len(ex_channelList) > 0:
                log_message = log_message + \
                    ",ex_channel=" + str(ex_channelList)
                # チャンネル選択初期値設定
                self.form_class.base_fields['ex_channelName'].initial = ex_channelList

            mode = self.request.GET.get('mode')
            if not mode:
                mode = '0'

            self.form_class.base_fields['mode'].initial = mode
            # if mode == "1":
            #    ### 緊急対応
            #    message = "現在一時的に全期間検索を機能制限しています。"
            #    return render(self.request, self.template_name,
            #                  {'form': self.get_form(), 'message': message})

            search_datatime_start_str = self.request.GET.get(
                'search_datatime_start')
            search_datatime_start = None
            if search_datatime_start_str:
                log_message = log_message + ",期間開始=" + \
                    str(search_datatime_start_str)
                self.form_class.base_fields['search_datatime_start'].initial = search_datatime_start_str
                search_datatime_start = datetime.strptime(
                    search_datatime_start_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone(timedelta(hours=+18)))
                if mode == '0':
                    # 直近モード
                    # logger.info("直近モード")
                    if search_datatime_start < nowtime - timedelta(days=7):
                        message = "検索期間は直近1週間以内で指定してください。"
                        return render(self.request, self.template_name,
                                      {'form': self.get_form(), 'message': message})

            else:
                if mode == '0':
                    search_datatime_start = nowtime - timedelta(days=7)
                else:
                    search_datatime_start = datetime.strptime(
                        '2018-02-01 00:00:00', '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone(timedelta(hours=+18)))

            search_datatime_end_str = self.request.GET.get(
                'search_datatime_end')
            search_datatime_end = None
            if search_datatime_end_str:
                log_message = log_message + ",期間終了=" + \
                    str(search_datatime_end_str)
                self.form_class.base_fields['search_datatime_end'].initial = search_datatime_end_str
                search_datatime_end = datetime.strptime(
                    search_datatime_end_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone(timedelta(hours=+18)))
                if mode == '0':
                    # 直近モード
                    if search_datatime_end < nowtime - timedelta(days=7):
                        message = "検索期間は直近1週間以内で指定してください。"
                        return render(self.request, self.template_name,
                                      {'form': self.get_form(), 'message': message})

                    if search_datatime_start and search_datatime_start > search_datatime_end:
                        message = "検索期間に誤りがあります。"
                        return render(self.request, self.template_name,
                                      {'form': self.get_form(), 'message': message})
            else:
                search_datatime_end = nowtime

            sort_mode = self.request.GET.get('sort_mode')
            if not sort_mode:
                sort_mode = '0'

            self.form_class.base_fields['sort_mode'].initial = sort_mode

            least_count = self.request.GET.get('least_count')
            if least_count:
                least_count = int(self.request.GET.get('least_count'))
            else:
                least_count = 0
            self.form_class.base_fields['least_count'].initial = least_count

            title_keyword = self.request.GET.get('title_keyword')
            if title_keyword:
                self.form_class.base_fields['title_keyword'].initial = title_keyword
            else:
                title_keyword = ''
                self.form_class.base_fields['title_keyword'].initial = title_keyword

            message = ""
            if keyword:
                log_message = log_message + ",mode=" + \
                    str(mode) + ",sort=" + str(sort_mode)
                logger.info(emoji.demojize(log_message))
                # キーワードごとに待機
                keyword_count = 0
                retry_count = 60
                while keyword_count <= retry_count:
                    obj = SearchKey.objects.filter(
                        keyword=emoji.demojize(keyword)).first()
                    check_now = datetime.now(timezone(timedelta(hours=9)))
                    if obj is None or check_now > obj.createdAt + timedelta(seconds=60):
                        break
                    keyword_count += 1
                    time.sleep(1)

                if keyword_count >= retry_count:
                    logger.info("検索中断:" + keyword)
                    message = "検索中です。再度お試しください。"
                    return render(self.request, self.template_name,
                                  {'form': self.get_form(), 'message': message})

                SearchKey.objects.create(
                    keyword=emoji.demojize(keyword), createdAt=nowtime)

                query = ""
                try:
                    queryset = None
                    videoList = None
                    queryset_list = []
                    target_date_list = []

                    # 一週間以内なら直近モードへ変更
                    if search_datatime_start and search_datatime_start >= nowtime - timedelta(days=7):
                        mode = '0'

                    if 'my_flag' in kwargs:
                        index = mycommentall_index
                    else:
                        index = commentall_index

                    # channelListの修正
                    if channelList:
                        tmp_channel_list = []
                        for channel in channelList:
                            try:
                                group_id = int(channel)
                                for ch in Channel.objects.filter(group_id=group_id):
                                    tmp_channel_list.append(ch.id)
                            except:
                                if channel not in tmp_channel_list:
                                    tmp_channel_list.append(channel)

                        channelList = tuple(tmp_channel_list)

                    # ex_channelListの修正
                    if ex_channelList:
                        tmp_channel_list = []
                        for channel in ex_channelList:
                            try:
                                group_id = int(channel)
                                for ch in Channel.objects.filter(group_id=group_id):
                                    tmp_channel_list.append(ch.id)
                            except:
                                if channel not in tmp_channel_list:
                                    tmp_channel_list.append(channel)

                        ex_channelList = tuple(tmp_channel_list)

                    result_summary = Empty()
                    # result_summary.video_count = 0 #廃止
                    # result_summary.comment_count =  0 #廃止
                    result_summary.max_flag = False
                    result_summary.zero_flag = True

                    page_obj = Empty()
                    page_number_str = request.GET.get('page')
                    if page_number_str and int(page_number_str) > 1:
                        page_obj.number = int(page_number_str)
                        page_obj.has_previous = True
                    else:
                        page_obj.number = 1
                        page_obj.has_previous = False

                    MAX_TRY = 1000

                    item_count = 0
                    page_hit_flag = False
                    after_key = None

                    timeset_flag = False
                    if mode == '1':
                        if sort_mode == '0':
                            req_search_datatime_start = search_datatime_start
                            search_datatime_start = search_datatime_end - \
                                relativedelta(months=1)
                            # 時間未指定の場合の強制期間制限
                            if not (req_search_datatime_start) or req_search_datatime_start < search_datatime_start:
                                timeset_flag = True
                            else:
                                search_datatime_start = req_search_datatime_start
                        elif sort_mode == '1':
                            req_search_datatime_end = search_datatime_end
                            search_datatime_end = search_datatime_start + \
                                relativedelta(months=1)
                            # 時間未指定の場合の強制期間制限
                            if not (req_search_datatime_end) or req_search_datatime_end > search_datatime_end:
                                timeset_flag = True
                            else:
                                search_datatime_end = req_search_datatime_end

                    if sort_mode == '2':
                        if page_obj.number > 5:
                            page_obj.number = 5

                    # 検索キーワード履歴登録
                    if page_obj.number == 1:
                        clientIP, _ = get_client_ip(request)
                        history = SearchHistory()
                        history.keyword = emoji.demojize(keyword)
                        if type == '1':
                            history.liverNameId_id = liverNameId
                        history.mode = mode
                        history.sort_mode = sort_mode
                        history.channelName = str(channelList)
                        history.ex_channelName = str(ex_channelList)
                        history.least_count = least_count
                        history.search_datatime_start_str = search_datatime_start_str
                        history.search_datatime_end_str = search_datatime_end_str

                        history.clientIP = clientIP
                        history.searchAt = nowtime
                        history.save()

                    # 時間によるタイムアウト
                    # TODO

                    for i in range(1, MAX_TRY):
                        es = elasticsearch.Elasticsearch(
                            "http://" + ES_HOST + ":9200", timeout=60)
                        query = make_video_query(keyword, liverNameId, channelList, ex_channelList,
                                                 search_datatime_start, search_datatime_end, after_key, sort_mode)
                        # logger.info("query" + json.dumps(query))
                        result = es.search(index=index, body=query)
                        after_key = result["aggregations"]["group_by_video_id"].get(
                            "after_key")
                        buckets = result["aggregations"]["group_by_video_id"]["buckets"]

                        result_dict = {}
                        video_id_list = []
                        for bucket in buckets:
                            if sort_mode == '2':
                                video_id = bucket['key']
                            else:
                                video_id = bucket['key']['video_id']
                            video_id_list.append(video_id)
                            doc_count = bucket['doc_count']
                            if doc_count >= least_count:
                                result_dict[video_id] = doc_count

                        if 'my_flag' in kwargs:
                            queryset = Video.objects.filter(
                                id__in=result_dict.keys())
                        else:
                            queryset = Video.objects.filter(
                                id__in=result_dict.keys(), enable=True)
                        video_list = []
                        if sort_mode == '0':
                            video_list = queryset.order_by('-publishedAt')
                        elif sort_mode == '1':
                            video_list = queryset.order_by('publishedAt')
                        elif sort_mode == '3':
                            video_list = queryset.order_by('-collectedAt')
                        elif sort_mode == '4':
                            video_list = queryset.order_by('collectedAt')
                        else:
                            for video_id in video_id_list:
                                for video in queryset:
                                    if video_id == video.id:
                                        video_list.append(video)

                        tmp_count = 0
                        for video in video_list:
                            tmp_count += 1
                            if 'my_flag' in kwargs:
                                if video is None:
                                    continue
                            else:
                                # if video is None or check_video(video) == False:
                                if video is None:
                                    continue
                                if not (video.enable and video.public) and check_video(video) == False:
                                    continue
                                '''
                                if "メン限" in video.title or "メンバーシップ限定" in video.title:
                                    # TODO 特殊対応　メン限が配信タイトルにあれば対象外とする
                                    continue
                                '''

                            # タイトル条件判定
                            # logger.info(video.title)
                            title_hit_flag = False
                            if title_keyword.replace('　', ' ').replace(' ', '') != '':
                                for word in title_keyword.replace('　', ' ').split(' '):
                                    # logger.info(word)
                                    if len(word) > 1 and word[0] == "-":
                                        if word[1:] in video.title:
                                            # logger.info("hit1" + word)
                                            title_hit_flag = False
                                            break
                                    elif len(word) > 0 and word in video.title:
                                        # logger.info("hit2" + word)
                                        title_hit_flag = True
                                if not title_hit_flag:
                                    continue

                            obj = Empty()
                            obj.img = "https://i3.ytimg.com/vi/" + video.id + "/mqdefault.jpg"
                            if 'my_flag' in kwargs:
                                if liverNameId:
                                    # liverNameIdの値はhtml側で足す
                                    obj.url = reverse('comment:comment_list_my', kwargs=dict(
                                        pk=video.id)) + "?liverNameId="
                                else:
                                    # keywrodの値はhtml側で足す
                                    obj.url = reverse('comment:comment_list_my', kwargs=dict(
                                        pk=video.id)) + "?keyword="
                            else:
                                if liverNameId:
                                    # liverNameIdの値はhtml側で足す
                                    obj.url = reverse('comment:comment_list_es', kwargs=dict(
                                        pk=video.id)) + "?liverNameId="
                                else:
                                    # keywrodの値はhtml側で足す
                                    obj.url = reverse('comment:comment_list_es', kwargs=dict(
                                        pk=video.id)) + "?keyword="

                            obj.video_id = video.id
                            if video.public and video.enable:
                                obj.title = emoji.emojize(video.title)
                            else:
                                obj.title = "★" + emoji.emojize(video.title)
                            obj.publishedAt = video.publishedAt
                            obj.channelName = emoji.emojize(
                                video.channel.channelName)
                            obj.count = result_dict[video.id]
                            self.object_list.append(obj)
                            result_summary.zero_flag = False

                            # PAGE_PER_ITEM超えていたら先頭の削除
                            if len(self.object_list) > PAGE_PER_ITEM:
                                self.object_list.pop(0)

                            item_count += 1
                            if (item_count-1)//PAGE_PER_ITEM == page_obj.number - 1 and item_count % PAGE_PER_ITEM == 0:
                                break

                        if after_key:
                            if i == MAX_TRY:
                                result_summary.max_flag = True
                            page_obj.has_next = True
                            if (item_count-1)//PAGE_PER_ITEM == page_obj.number - 1 and item_count % PAGE_PER_ITEM == 0:
                                # page分の取得完了
                                break
                        else:
                            if timeset_flag:
                                # 次の期間で再度コメント取得
                                if sort_mode == '0':
                                    search_datatime_end = search_datatime_start
                                    search_datatime_start = search_datatime_end - \
                                        relativedelta(months=1)
                                    if search_datatime_start < req_search_datatime_start:
                                        timeset_flag = False
                                        search_datatime_start = req_search_datatime_start
                                elif sort_mode == '1':
                                    search_datatime_start = search_datatime_end
                                    search_datatime_end = search_datatime_start + \
                                        relativedelta(months=1)
                                    if search_datatime_end > req_search_datatime_end:
                                        timeset_flag = False
                                        search_datatime_end = req_search_datatime_end
                            else:
                                if tmp_count < len(queryset):
                                    # queryset回りきっていない
                                    page_obj.has_next = True
                                else:
                                    page_obj.number = (
                                        item_count - 1) // PAGE_PER_ITEM + 1
                                    page_obj.has_next = False
                                # データの取得完了
                                break

                    # 不要分の削除
                    for i in range(1, len(self.object_list)):
                        if len(self.object_list) > (item_count - 1) % PAGE_PER_ITEM + 1:
                            self.object_list.pop(0)

                    page_obj.previous_page_number = page_obj.number - 1
                    page_obj.next_page_number = page_obj.number + 1
                    page_obj.next_10page_number = page_obj.number + 10

                    if sort_mode == '2' and page_obj.number == 5:
                        page_obj.has_next = False

                    '''
                    if page_obj.number - 5 <= 1:
                        page_min = 1
                    else:
                        page_min = page_obj.number - 5
                    if page_min + 9 <= 10:
                        page_max = 10
                    else:
                        page_max = page_min + 9
                    '''
                    page_obj.paginator = Empty()
                    page_obj.paginator.page_range = range(
                        page_obj.number, page_obj.number+1)

                    if page_obj.number - 10 > 0:
                        page_obj.previous_10page_number = page_obj.number - 10

                    if page_obj.number == 1:
                        page_obj.has_previous = False
                    page_obj.url_options = ""
                    for key, value in self.request.GET.items():
                        if key == 'page':
                            continue
                        if key == 'channelName' or key == 'ex_channelName':
                            for channel in self.request.GET.getlist(key):
                                page_obj.url_options += "&" + \
                                    key + "=" + str(channel)
                            continue
                        page_obj.url_options += "&" + key + "=" + str(value)

                except elasticsearch.ConnectionTimeout as e:
                    logger.info("query" + json.dumps(query))
                    logger.error("ESタイムアウト:" + keyword, exc_info=True)
                    SearchKey.objects.filter(
                        keyword=emoji.demojize(keyword)).delete()

                    message = "検索結果が多いか、アクセスが集中しています。期間などを絞り込むか、時間をおいてお試しください。"

                    return render(self.request, self.template_name,
                                  {'form': self.get_form(), 'message': message})
                except:
                    logger.info("query" + json.dumps(query))
                    logger.error("想定外エラー:" + keyword, exc_info=True)
                    SearchKey.objects.filter(
                        keyword=emoji.demojize(keyword)).delete()

                    message = "エラーが発生しました。お手数おかけしますが事象が解消されない場合は問い合わせをお願い致します。"
                    return render(self.request, self.template_name,
                                  {'form': self.get_form(), 'message': message})

                SearchKey.objects.filter(
                    keyword=emoji.demojize(keyword)).delete()

                logger.info("検索完了:" + emoji.demojize(keyword))
            else:
                self.object_list = Video.objects.none()

            response = render(self.request, self.template_name, {'form': self.get_form(
            ), 'object_list': self.object_list, 'page_obj': page_obj, 'result_summary': result_summary, 'liver_list': liver_list, 'liverNameId': liverNameId, 'my_flag': my_flag})
            return response

    def get_queryset(self):
        return Video.objects.none()


class VideoListMY(VideoListES):
    # @login_required
    def get(self, request, *args, **kwargs):
        form_class = SearchFormMYES  # TODO うまくいかない
        kwargs['my_flag'] = True
        response = super().get(request, *args, **kwargs)
        return response


class VideoListONLY(VideoListES):
    # @login_required
    def get(self, request, *args, **kwargs):
        form_class = SearchFormMYES  # TODO うまくいかない
        kwargs['only_flag'] = True
        response = super().get(request, *args, **kwargs)
        return response


def livername_view(request):
    livernames = LiverName.objects.filter(id__gt=121).order_by('no')

    groups = []

    group = Empty()
    group.id = 1
    group.name = "test"
    group.names = []
    for livername in livernames:
        name = Empty()
        name.id = livername.id
        name.name = livername.liver_name
        name.types = parse_livername_keyword(livername.keyword)
        name.non = (len(name.types) == 2)
        group.names.append(name)

    groups.append(group)

    return render(request,
                  'comment/livername_list.html',     # 使用するテンプレート
                  {'groups': groups})         # テンプレートに渡すデータ


def livername_submit(request):
    if request.method == 'POST':
        data = json.loads(request.POST.get('data'))
        for i in range(len(data)):
            print(f'Text Box {i+1}: {data[i]["input"]}')
            print(f'Check Box {i+1}: {data[i]["checkbox"]}')
    return render(request, 'comment/livername_list.html')


def parse_livername_keyword(keyword):
    not_word_list = []
    word_list = []
    for word in keyword.replace('　', ' ').split(' '):
        if len(word) == 0:
            continue
        if word[0] == '-':
            not_word_list.append(word[1:])
        else:
            word_list.append(word)

    types = []

    type = Empty()
    type.name = "キーワード"
    type.value = '/'.join(word_list)
    types.append(type)

    if len(not_word_list) > 0:
        type = Empty()
        type.name = "除外ワード"
        type.value = '/'.join(not_word_list)
        types.append(type)

    return types


def check_keyword(keyword):

    count = 0
    for word in keyword.replace('　', ' ').split(' '):
        if not (word):
            continue

        if word == "*":
            return False
        if word == "-":
            return False
        if word == "-*":
            return False
        if word == "\t":
            return False
        if word[0] != "-":
            count += 1

    if count == 0:
        return False

    return True


def make_base_query(keyword, liverNameId):
    not_word_list = []
    word_list = []
    for word in keyword.replace('　', ' ').split(' '):
        # 様子を見ていく
        # type = "wildcard"
        type = "ngram"
        if not (word):
            continue

        if wildcard_check(word):
            type = "wildcard"
            word = word.replace('?', '\?').replace('*', '\*')

        if type == "wildcard":
            if word[0] == '-':
                not_word_list.append(
                    {
                        "wildcard": {
                            "message.wildcard": {
                                "value": "*" + word[1:] + "*"
                            }
                        }
                    }
                )
            else:
                word_list.append(
                    {
                        "wildcard": {
                            "message.wildcard": {
                                "value": "*" + word + "*"
                            }
                        }
                    }
                )
        else:
            target = "message"
            if jp_check(word):
                target = target + ".ngram"

            if word[0] == '-':
                not_word_list.append(
                    {
                        "match_phrase": {
                            target: word[1:]
                        }
                    }
                )
            else:
                word_list.append(
                    {
                        "match_phrase": {
                            target: word
                        }
                    }
                )

    query = {
        "query": {
            "bool": {
                "must": []
            }
        }
    }

    if liverNameId:
        query['query']['bool']['must'].append(
            {
                "bool": {
                    "should": word_list,
                    "minimum_should_match": 1
                }
            }
        )
    else:
        query['query']['bool']['must'].append(
            {
                "bool": {
                    "must": word_list
                }
            }
        )

    if not_word_list:
        query['query']['bool']['must'].append(
            {
                "bool": {
                    "must_not": not_word_list
                }
            }
        )
    return query


def jp_check(word):

    # 特別対応
    sp_word_list = ["魔界ノ"]
    for sp_word in sp_word_list:
        if word == sp_word:
            return True

    for ch in word:
        name = unicodedata.name(ch)
        if "CJK KATAKANA" in name or "HIRAGANA" in name:
            # if "CJK KATAKANA" in name or "HIRAGANA" in name or "UNIFIED" in name:
            return True
    return False


def wildcard_check(word):
    # 絵文字とハングルはワイルドカード

    if emoji.emoji_count(word) > 0:
        return True

    if re.search('[가-힣]', word):
        return True

    if re.search('[!-/:-@\[-`{-~！-／：-＠［-｀｛-～、-〜”’・]', word):
        return True

    return False


def make_video_query(keyword, liverNameId, channelList, ex_channelList, search_datatime_start, search_datatime_end, after_key, sort_mode):
    query = make_base_query(keyword, liverNameId)

    if sort_mode == '0' or sort_mode == '1':
        # 配信日降順/昇順
        if sort_mode == '0':
            order = "desc"
        else:
            order = "asc"
        query['sort'] = [
            {
                "video_publishedat": {"order": order}
            }
        ]
    if sort_mode == '3' or sort_mode == '4':
        # 配信日降順/昇順
        if sort_mode == '3':
            order = "desc"
        else:
            order = "asc"
        query['sort'] = [
            {
                "video_collectedat": {"order": order}
            }
        ]

    if channelList and len(channelList) > 0:
        should = []
        for channel in channelList:
            should.append(
                {
                    "match": {
                        "video_channel_id": {
                            "query": channel
                        }
                    }
                }
            )
        query['query']['bool']['must'].append(
            {
                "bool": {
                    "should": should
                }
            }
        )

    if ex_channelList and len(ex_channelList) > 0:
        must_not = []
        for channel in ex_channelList:
            must_not.append(
                {
                    "match": {
                        "video_channel_id": {
                            "query": channel
                        }
                    }
                }
            )
        query['query']['bool']['must'].append(
            {
                "bool": {
                    "must_not": must_not
                }
            }
        )

    # 期間指定
    if search_datatime_start:
        query['query']['bool']['must'].append(
            {
                "range": {
                    "video_publishedat": {
                        "gt": int(search_datatime_start.timestamp()) * 1000
                    }
                }
            }
        )
    if search_datatime_end:
        query['query']['bool']['must'].append(
            {
                "range": {
                    "video_publishedat": {
                        "lte": int(search_datatime_end.timestamp()) * 1000
                    }
                }
            }
        )

    # 取得絡む指定
    if sort_mode == '0' or sort_mode == '1':
        query['size'] = 0
        query['aggs'] = {
            "group_by_video_id": {
                "composite": {
                    "size": PAGE_PER_ITEM,
                    "sources": [
                        {
                            "video_publishedat": {
                                "terms": {
                                    "field": "video_publishedat",
                                    "order": order
                                }
                            }
                        },
                        {
                            "video_id": {
                                "terms": {
                                    "field": "video_id"
                                }
                            }
                        }
                    ]
                }
            }
        }
    elif sort_mode == '3' or sort_mode == '4':
        query['size'] = 0
        query['aggs'] = {
            "group_by_video_id": {
                "composite": {
                    "size": PAGE_PER_ITEM,
                    "sources": [
                        {
                            "video_publishedat": {
                                "terms": {
                                    "field": "video_collectedat",
                                    "order": order
                                }
                            }
                        },
                        {
                            "video_id": {
                                "terms": {
                                    "field": "video_id"
                                }
                            }
                        }
                    ]
                }
            }
        }
    else:
        query['size'] = 0
        query['aggs'] = {
            "group_by_video_id": {
                "terms": {
                    "field": "video_id",
                    "size": 100
                }
            }
        }

    if after_key:
        query['aggs']['group_by_video_id']['composite']['after'] = after_key

    return query


def livername_get_keyword(liverNameId):
    return LiverName.objects.get(pk=liverNameId).keyword


def livername_get_liver_name(liverNameId):
    return LiverName.objects.get(pk=liverNameId).liver_name


def api_suggests_get(request):
    keyword = request.GET.get('keyword')
    if keyword:
        object_list = [{'pk': channel.pk,
                        'name': str(channel),
                        'name_kana': str(channel)}
                       for channel in Channel.objects.filter(channelName__search=keyword)]
    else:
        object_list = []
    return JsonResponse({'object_list': object_list})


@login_required
def channel_list(request):
    """チャンネルの一覧"""
    # return HttpResponse('チャンネルの一覧')
    channels = Channel.objects.all().order_by('no')
    return render(request,
                  'comment/channel_list.html',     # 使用するテンプレート
                  {'channels': channels})         # テンプレートに渡すデータ


@login_required
def channel_edit(request, channel_id=None):
    """チャンネルの編集"""
    # return HttpResponse('チャンネルの編集')
    logger.info(channel_id)
    create_flag = False
    if channel_id:   # channel_id が指定されている (修正時)
        channel = get_object_or_404(Channel, pk=channel_id)
    else:         # channel_id が指定されていない (追加時)
        channel = Channel()
        create_flag = True

    if request.method == 'POST':
        # POST された request データからフォームを作成
        form = ChannelForm(request.POST, instance=channel)
        if form.is_valid():    # フォームのバリデーション
            channel = form.save(commit=False)
            channel_name = yt.get_channel_name(channel.id)
            channel.channelName = channel_name

            channel.collecting_flag = True
            if create_flag:
                max = Channel.objects.all().aggregate(Max('no'))
                channel.no = max['no__max'] + 1
                # channel.group_id = 1  # デフォルトで1に設定　その後手動で変更
            channel.save()

            # 動画一覧取得
            future = executor.submit(collet_videolist, channel)
            # collet_videolist(channel)

            return redirect('comment:channel_list')
    else:    # GET の時
        form = ChannelForm(instance=channel)  # channel インスタンスからフォームを作成

    return render(request, 'comment/channel_edit.html', dict(form=form, channel_id=channel_id))


@login_required
def channel_del(request, channel_id):
    """チャンネルの削除"""
    # return HttpResponse('チャンネルの削除')
    channel = get_object_or_404(Channel, pk=channel_id)
    channel.delete()
    return redirect('comment:channel_list')


def collet_videolist(channel, new_flag=True, db_only_flag=False):

    try:
        if not (db_only_flag):
            # 動画一覧情報
            etag, infos = yt.get_video_id_list_by_playlist_id(
                channel.id, not (new_flag), channel.etag)
            channel.etag = etag
            channel.save()
            video_id_list = []

            for video_id in infos:
                video = Video.objects.filter(id=video_id).first()
                if video is None:
                    if video_id not in video_id_list:
                        video_id_list.append(video_id)
                else:
                    check_video(video)

            infos = yt.get_video_snippet_list(video_id_list)
            db_new_video_list = []
            for info in infos:
                [video_id, item] = info

                title = item['snippet']['title']
                publishedAt = item['snippet']['publishedAt']

                video = Video()
                video.channel = channel
                video.id = video_id
                video.title = unquote(emoji.demojize(title))
                video.description = ""
                video.publishedAt = datetime.strptime(publishedAt, '%Y-%m-%dT%H:%M:%SZ').replace(
                    tzinfo=timezone(timedelta(hours=+9)))
                video.enable = True
                video.public = True
                video.collecting_flag = True
                # video.save()

                if 'liveStreamingDetails' in item:
                    if 'scheduledStartTime' in item['liveStreamingDetails']:
                        video.scheduledStartTime = item['liveStreamingDetails']['scheduledStartTime']
                else:
                    logger.info("非配信:" + video.id, exc_info=True)
                    video.enable = False

                db_new_video_list.append(video)

            Video.objects.bulk_create(db_new_video_list)
    except:
        logger.info("想定外エラー:" + channel.id, exc_info=True)
    logger.info("collet_videolist完了：" + channel.id)


# ページネーション用に、Pageオブジェクトを返す。
def paginate_query(request, queryset, count):
    paginator = Paginator(queryset, count)
    page = request.GET.get('page')
    try:
        page_obj = paginator.page(page)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)
    return page_obj


class Search(models.Lookup):
    lookup_name = 'search'

    def as_mysql(self, compiler, connection):
        lhs, lhs_params = self.process_lhs(compiler, connection)
        rhs, rhs_params = self.process_rhs(compiler, connection)
        params = lhs_params + rhs_params
        return 'MATCH (%s) AGAINST (%s IN BOOLEAN MODE)' % (lhs, rhs), params


models.CharField.register_lookup(Search)
models.TextField.register_lookup(Search)


class PolicyView(TemplateView):
    template_name = "comment/policy.html"


class AboutView(TemplateView):
    template_name = "comment/about.html"

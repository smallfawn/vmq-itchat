import io
import json
import logging
import os
import random
import re
import threading
import time
import traceback

try:
    from httplib import BadStatusLine
except ImportError:
    from http.client import BadStatusLine

import requests

from .. import config, utils
from ..returnvalues import ReturnValue
from ..storage.templates import wrap_user_dict
from .contact import update_local_chatrooms, update_local_friends
from .messages import produce_msg

log = logging.getLogger('itchat')

def load_config():
    # 获取当前脚本所在目录
    current_dir = os.path.dirname(os.path.abspath(__file__))

    # 构建配置文件的绝对路径
    config_path = os.path.join(current_dir, '../../config.json')

    # 加载配置
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
    except FileNotFoundError:
        log.error(f"配置文件 '{config_path}' 未找到")
        raise
    return config

def send_to_wechat_bot(content, webhook_url):
    headers = {'Content-Type': 'application/json'}
    data = {
        "msgtype": "text",
        "text": {
            "content": content
        }
    }
    try:
        response = requests.post(webhook_url, headers=headers, data=json.dumps(data))
        if response.status_code != 200:
            log.error(f"Failed to send message to WeChat bot: {response.status_code}, {response.text}")
    except Exception as e:
        log.error(f"Error sending message to WeChat bot: {str(e)}")

def load_login(core):
    core.login = login
    core.get_QRuuid = get_QRuuid
    core.check_login = check_login
    core.web_init = web_init
    core.show_mobile_login = show_mobile_login
    core.start_receiving = start_receiving
    core.get_msg = get_msg
    core.logout = logout


def login(self, loginCallback=None, exitCallback=None):
    webhook_url = load_config().get('webhook_url')

    if self.alive or self.isLogging:
        log.warning('itchat has already logged in.')
        return
    self.isLogging = True
    while self.isLogging:
        uuid = push_login(self)
        if uuid:
            qr_url = f"https://qrcode-devcxl.pages.dev/?url=https://login.weixin.qq.com/l/{uuid}"
            log.info(qr_url)
            send_to_wechat_bot(f"已过期，请扫描二维码登录：{qr_url}", webhook_url)
        else:
            self.get_QRuuid()
            qr_url = f"https://qrcode-devcxl.pages.dev/?url=https://login.weixin.qq.com/l/{self.uuid}"
            log.info(qr_url)
            log.info('Please scan the QR code to log in.')
            send_to_wechat_bot(f"请扫描二维码登录：{qr_url}", webhook_url)
        isLoggedIn = False
        while not isLoggedIn:
            status = self.check_login()
            if status == '200':
                isLoggedIn = True
            elif status == '201':
                if isLoggedIn is not None:
                    log.info('Please press confirm on your phone.')
                    isLoggedIn = None
                time.sleep(0.5)
            elif status != '408':
                break
        if isLoggedIn:
            break
        elif self.isLogging:
            log.info('Log in time out, reloading QR code.')
    else:
        return  # log in process is stopped by user
    log.info('Loading the contact, this may take a little while.')
    self.web_init()
    self.show_mobile_login()
    self.get_contact(True)
    if hasattr(loginCallback, '__call__'):
        r = loginCallback()
    else:
        log.info('Login successfully as %s' % self.storageClass.nickName)
    self.start_receiving(exitCallback)
    self.isLogging = False


def push_login(core):
    cookiesDict = core.s.cookies.get_dict()
    if 'wxuin' in cookiesDict:
        url = '%s/cgi-bin/mmwebwx-bin/webwxpushloginurl?uin=%s' % (
            config.BASE_URL, cookiesDict['wxuin'])
        headers = {'User-Agent': config.USER_AGENT}
        r = core.s.get(url, headers=headers).json()
        if 'uuid' in r and r.get('ret') in (0, '0'):
            core.uuid = r['uuid']
            return r['uuid']
    return False


def get_QRuuid(self):
    url = '%s/jslogin' % config.BASE_URL
    params = {
        'appid': 'wx782c26e4c19acffb',
        'fun': 'new',
        'redirect_uri': 'https://wx.qq.com/cgi-bin/mmwebwx-bin/webwxnewloginpage?mod=desktop',
        'lang': 'zh_CN'}
    headers = {'User-Agent': config.USER_AGENT}
    r = self.s.get(url, params=params, headers=headers)
    regx = r'window.QRLogin.code = (\d+); window.QRLogin.uuid = "(\S+?)";'
    data = re.search(regx, r.text)
    if data and data.group(1) == '200':
        self.uuid = data.group(2)
        return self.uuid


def check_login(self, uuid=None):
    uuid = uuid or self.uuid
    url = '%s/cgi-bin/mmwebwx-bin/login' % config.BASE_URL
    localTime = int(time.time())
    params = 'loginicon=true&uuid=%s&tip=1&r=%s&_=%s' % (
        uuid, int(-localTime / 1579), localTime)
    headers = {'User-Agent': config.USER_AGENT}
    r = self.s.get(url, params=params, headers=headers)
    regx = r'window.code=(\d+)'
    data = re.search(regx, r.text)
    if data and data.group(1) == '200':
        if process_login_info(self, r.text):
            return '200'
        else:
            return '400'
    elif data:
        return data.group(1)
    else:
        return '400'


def process_login_info(core, loginContent):
    ''' when finish login (scanning qrcode)
     * syncUrl and fileUploadingUrl will be fetched
     * deviceid and msgid will be generated
     * skey, wxsid, wxuin, pass_ticket will be fetched
    '''
    regx = r'window.redirect_uri="(\S+)";'
    core.loginInfo['url'] = re.search(regx, loginContent).group(1)
    headers = {'User-Agent': config.USER_AGENT,
               'client-version': config.UOS_PATCH_CLIENT_VERSION,
               'extspam': config.UOS_PATCH_EXTSPAM,
               'referer': 'https://wx.qq.com/?&lang=zh_CN&target=t'
               }
    r = core.s.get(core.loginInfo['url'],
                   headers=headers, allow_redirects=False)
    core.loginInfo['url'] = core.loginInfo['url'][:core.loginInfo['url'].rfind(
        '/')]
    for indexUrl, detailedUrl in (
            ("wx2.qq.com", ("file.wx2.qq.com", "webpush.wx2.qq.com")),
            ("wx8.qq.com", ("file.wx8.qq.com", "webpush.wx8.qq.com")),
            ("qq.com", ("file.wx.qq.com", "webpush.wx.qq.com")),
            ("web2.wechat.com", ("file.web2.wechat.com", "webpush.web2.wechat.com")),
            ("wechat.com", ("file.web.wechat.com", "webpush.web.wechat.com"))):
        fileUrl, syncUrl = ['https://%s/cgi-bin/mmwebwx-bin' %
                            url for url in detailedUrl]
        if indexUrl in core.loginInfo['url']:
            core.loginInfo['fileUrl'], core.loginInfo['syncUrl'] = \
                fileUrl, syncUrl
            break
    else:
        core.loginInfo['fileUrl'] = core.loginInfo['syncUrl'] = core.loginInfo['url']
    core.loginInfo['deviceid'] = 'e' + repr(random.random())[2:17]
    core.loginInfo['logintime'] = int(time.time() * 1e3)
    core.loginInfo['BaseRequest'] = {}
    cookies = core.s.cookies.get_dict()
    skey = re.findall('<skey>(.*?)</skey>', r.text, re.S)[0]
    pass_ticket = re.findall(
        '<pass_ticket>(.*?)</pass_ticket>', r.text, re.S)[0]
    core.loginInfo['skey'] = core.loginInfo['BaseRequest']['Skey'] = skey
    core.loginInfo['wxsid'] = core.loginInfo['BaseRequest']['Sid'] = cookies["wxsid"]
    core.loginInfo['wxuin'] = core.loginInfo['BaseRequest']['Uin'] = cookies["wxuin"]
    core.loginInfo['pass_ticket'] = pass_ticket
    # A question : why pass_ticket == DeviceID ?
    #               deviceID is only a randomly generated number

    # UOS PATCH By luvletter2333, Sun Feb 28 10:00 PM
    # for node in xml.dom.minidom.parseString(r.text).documentElement.childNodes:
    #     if node.nodeName == 'skey':
    #         core.loginInfo['skey'] = core.loginInfo['BaseRequest']['Skey'] = node.childNodes[0].data
    #     elif node.nodeName == 'wxsid':
    #         core.loginInfo['wxsid'] = core.loginInfo['BaseRequest']['Sid'] = node.childNodes[0].data
    #     elif node.nodeName == 'wxuin':
    #         core.loginInfo['wxuin'] = core.loginInfo['BaseRequest']['Uin'] = node.childNodes[0].data
    #     elif node.nodeName == 'pass_ticket':
    #         core.loginInfo['pass_ticket'] = core.loginInfo['BaseRequest']['DeviceID'] = node.childNodes[0].data
    if not all([key in core.loginInfo for key in ('skey', 'wxsid', 'wxuin', 'pass_ticket')]):
        log.error(
            'Your wechat account may be LIMITED to log in WEB wechat, error info:\n%s' % r.text)
        core.isLogging = False
        return False
    return True


def web_init(self):
    url = '%s/webwxinit' % self.loginInfo['url']
    params = {
        'r': int(-time.time() / 1579),
        'pass_ticket': self.loginInfo['pass_ticket'], }
    data = {'BaseRequest': self.loginInfo['BaseRequest'], }
    headers = {
        'ContentType': 'application/json; charset=UTF-8',
        'User-Agent': config.USER_AGENT, }
    r = self.s.post(url, params=params, data=json.dumps(data), headers=headers)
    dic = json.loads(r.content.decode('utf-8', 'replace'))
    # deal with login info
    utils.emoji_formatter(dic['User'], 'NickName')
    self.loginInfo['InviteStartCount'] = int(dic['InviteStartCount'])
    self.loginInfo['User'] = wrap_user_dict(
        utils.struct_friend_info(dic['User']))
    self.memberList.append(self.loginInfo['User'])
    self.loginInfo['SyncKey'] = dic['SyncKey']
    self.loginInfo['synckey'] = '|'.join(['%s_%s' % (item['Key'], item['Val'])
                                          for item in dic['SyncKey']['List']])
    self.storageClass.userName = dic['User']['UserName']
    self.storageClass.nickName = dic['User']['NickName']
    # deal with contact list returned when init
    contactList = dic.get('ContactList', [])
    chatroomList, otherList = [], []
    for m in contactList:
        if m['Sex'] != 0:
            otherList.append(m)
        elif '@@' in m['UserName']:
            m['MemberList'] = []  # don't let dirty info pollute the list
            chatroomList.append(m)
        elif '@' in m['UserName']:
            # mp will be dealt in update_local_friends as well
            otherList.append(m)
    if chatroomList:
        update_local_chatrooms(self, chatroomList)
    if otherList:
        update_local_friends(self, otherList)
    return dic


def show_mobile_login(self):
    url = '%s/webwxstatusnotify?lang=zh_CN&pass_ticket=%s' % (
        self.loginInfo['url'], self.loginInfo['pass_ticket'])
    data = {
        'BaseRequest': self.loginInfo['BaseRequest'],
        'Code': 3,
        'FromUserName': self.storageClass.userName,
        'ToUserName': self.storageClass.userName,
        'ClientMsgId': int(time.time()), }
    headers = {
        'ContentType': 'application/json; charset=UTF-8',
        'User-Agent': config.USER_AGENT, }
    r = self.s.post(url, data=json.dumps(data), headers=headers)
    return ReturnValue(rawResponse=r)


def start_receiving(self, exitCallback=None, getReceivingFnOnly=False):
    self.alive = True

    def maintain_loop():
        retryCount = 0
        while self.alive:
            try:
                i = sync_check(self)
                if i is None:
                    self.alive = False
                elif i == '0':
                    pass
                else:
                    msgList, contactList = self.get_msg()
                    if msgList:
                        msgList = produce_msg(self, msgList)
                        for msg in msgList:
                            if not self.storageClass.history_check(msg['MsgId']):
                                self.storageClass.append_history(msg['MsgId'])
                                self.msgList.put(msg)
                    if contactList:
                        chatroomList, otherList = [], []
                        for contact in contactList:
                            if '@@' in contact['UserName']:
                                chatroomList.append(contact)
                            else:
                                otherList.append(contact)
                        chatroomMsg = update_local_chatrooms(
                            self, chatroomList)
                        chatroomMsg['User'] = self.loginInfo['User']
                        self.msgList.put(chatroomMsg)
                        update_local_friends(self, otherList)
                retryCount = 0
            except requests.exceptions.ReadTimeout:
                pass
            except:
                retryCount += 1
                log.error(traceback.format_exc())
                if self.receivingRetryCount < retryCount:
                    self.alive = False
                else:
                    time.sleep(1)
        self.logout()
        if hasattr(exitCallback, '__call__'):
            exitCallback()
        else:
            log.info('LOG OUT!')

    if getReceivingFnOnly:
        return maintain_loop
    else:
        maintainThread = threading.Thread(target=maintain_loop)
        maintainThread.setDaemon(True)
        maintainThread.start()


def sync_check(self):
    url = '%s/synccheck' % self.loginInfo.get('syncUrl', self.loginInfo['url'])
    params = {
        'r': int(time.time() * 1000),
        'skey': self.loginInfo['skey'],
        'sid': self.loginInfo['wxsid'],
        'uin': self.loginInfo['wxuin'],
        'deviceid': self.loginInfo['deviceid'],
        'synckey': self.loginInfo['synckey'],
        '_': self.loginInfo['logintime'], }
    headers = {'User-Agent': config.USER_AGENT}
    self.loginInfo['logintime'] += 1
    try:
        r = self.s.get(url, params=params, headers=headers,
                       timeout=config.TIMEOUT)
    except requests.exceptions.ConnectionError as e:
        try:
            if not isinstance(e.args[0].args[1], BadStatusLine):
                raise
            # will return a package with status '0 -'
            # and value like:
            # 6f:00:8a:9c:09:74:e4:d8:e0:14:bf:96:3a:56:a0:64:1b:a4:25:5d:12:f4:31:a5:30:f1:c6:48:5f:c3:75:6a:99:93
            # seems like status of typing, but before I make further achievement code will remain like this
            return '2'
        except:
            raise
    r.raise_for_status()
    regx = r'window.synccheck={retcode:"(\d+)",selector:"(\d+)"}'
    pm = re.search(regx, r.text)
    if pm is None or pm.group(1) != '0':
        log.debug('Unexpected sync check result: %s' % r.text)
        return None
    return pm.group(2)


def get_msg(self):
    self.loginInfo['deviceid'] = 'e' + repr(random.random())[2:17]
    url = '%s/webwxsync?sid=%s&skey=%s&pass_ticket=%s' % (
        self.loginInfo['url'], self.loginInfo['wxsid'],
        self.loginInfo['skey'], self.loginInfo['pass_ticket'])
    data = {
        'BaseRequest': self.loginInfo['BaseRequest'],
        'SyncKey': self.loginInfo['SyncKey'],
        'rr': ~int(time.time()), }
    headers = {
        'ContentType': 'application/json; charset=UTF-8',
        'User-Agent': config.USER_AGENT}
    r = self.s.post(url, data=json.dumps(data),
                    headers=headers, timeout=config.TIMEOUT)
    dic = json.loads(r.content.decode('utf-8', 'replace'))
    if dic['BaseResponse']['Ret'] != 0:
        return None, None
    self.loginInfo['SyncKey'] = dic['SyncKey']
    self.loginInfo['synckey'] = '|'.join(['%s_%s' % (item['Key'], item['Val'])
                                          for item in dic['SyncCheckKey']['List']])
    return dic['AddMsgList'], dic['ModContactList']


def logout(self):
    if self.alive:
        url = '%s/webwxlogout' % self.loginInfo['url']
        params = {
            'redirect': 1,
            'type': 1,
            'skey': self.loginInfo['skey'], }
        headers = {'User-Agent': config.USER_AGENT}
        self.s.get(url, params=params, headers=headers)
        self.alive = False
    self.isLogging = False
    self.s.cookies.clear()
    del self.chatroomList[:]
    del self.memberList[:]
    del self.mpList[:]
    return ReturnValue({'BaseResponse': {
        'ErrMsg': 'logout successfully.',
        'Ret': 0, }})
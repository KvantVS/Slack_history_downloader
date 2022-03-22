import json
import re
import requests as req
import zlib
from os import makedirs, chdir
from os.path import join, exists, splitext, dirname
from datetime import datetime, timezone
from time import sleep, time
from functools import partial
from multiprocessing.dummy import Pool as ThreadPool
from subprocess import Popen


# Consts
TOKEN = ''
HOST = 'https://slack.com/api'
MESSAGES_PER_REQUEST = 600 # 600
SLEEP_TIME = 0.4

# globals
files = []
users = {}


class ChannelNotFoundException(Exception):
    pass

class OtherException(Exception):
    pass


def get_replies(ses, thread_ts, client_msg_id):
    params = {
        'channel': CHANNEL,
        'limit': MESSAGES_PER_REQUEST,
        'ts': thread_ts
    }

    replies = get_messages(ses, 'conversations.replies', params, client_msg_id, False)

    # Удаляем первое сообщение, т.к. оно равно самому сообщению вне ответов
    for repl_i in range(len(replies)):
        msg = replies[repl_i]
        if msg['ts'] == thread_ts:
            del replies[repl_i]
            break

    return replies


def http_get(ses, method, params):
    while True:
        try:
            r = ses.get(f'{HOST}/{method}', params=params)
        except Exception as e:
            print(f'Возникла ошибка при запросе {HOST}/{method}:')
            print(e)
            print('Спим 30 секунд...')
            sleep(30)
            continue

        if r.status_code != 200:
            print('Status code =', r.status_code)
            if r.status_code == 429:
                print('Слишком много запросов. Спим 30 секунд...')
                sleep(30)
                continue
            else:
                print('Retry in 30 seconds...')
                sleep(30)

        j = r.json()
        if not j['ok']:
            if j['error'] == 'channel_not_found':
                raise ChannelNotFoundException('Slack error: ' + j['error'])
            else:
                raise OtherException('Slack error: ' + j['error'])

        return j


def convert_ts_into_local_timezone(ts):
    '''Конвертирует timestamp ts в датувремя, но с прибавленным часовым поясом системы'''

    if '.' in ts:
        l = ts.split('.')
        if len(l) != 2:
            raise ValueError('Invalid time stamp')
    dt = datetime.utcfromtimestamp(int(l[0])).replace(tzinfo=timezone.utc)
    dt = dt.astimezone()
    return dt


def get_user_from_slack(user_id):
    '''Получаем юзера из базы Слака, и заносим его в глобальный список'''

    j_user = http_get(ses, 'users.info', {'user': user_id})['user']
    if 'real_name' in j_user:
        users[user_id] = j_user['real_name']
    elif 'profile' in j_user and 'real_name' in j_user['profile']:
        users[user_id] = j_user['profile']['real_name']
    elif 'profile' in j_user and 'display_name' in j_user['profile']:
        users[user_id] = j_user['profile']['display_name']
    
    # Скачаем фото юзера, если нету
    makedirs('users', exist_ok=True)
    user_filename = join('users', user_id)
    if not exists(user_filename):
        if 'image_192' in j_user['profile']:  # image_original
            r = ses_users.get(str(j_user['profile']['image_192']))
            if r.status_code != 200:
                print(r)
                print(j_user['profile']['image_192'])
                print(f'Не могу скачать фото юзера {users[user_id]} id: {user_id}')
            else:
                with open(user_filename, 'wb') as userfile:
                    userfile.write(r.content)


def get_messages(ses, method, params, client_msg_id=None, do_print=True):
    '''Основная процедура, получаем json сообщений, обрабатываем их'''
    cursor = ''
    messages = []
    current_date = 0
    msgs_count = 0
    global files
    global users

    while True:
        if cursor: 
            params.update(cursor=cursor)

        j = http_get(ses, method, params)
        sleep(SLEEP_TIME)

        # Запомним за какой день обрабатываем сообщения
        if len(j['messages']) > 0:
            msgs_count += len(j['messages'])
            current_date = j['messages'][0]['ts']
            current_date = convert_ts_into_local_timezone(current_date).date()

        for msg in j['messages']:
            # Пропускаем, если зашли сюда из get_replies и обрабатываем родительское сообщение
            if 'client_msg_id' in msg and (msg['client_msg_id'] == client_msg_id):
                continue

            # Заходим в "обсуждение" сообщения, достаём все сообщения оттуда, добавляем в само сообщение
            if 'reply_count' in msg:
                if int(msg['reply_count']) > 0:
                    thread_replies = get_replies(ses, msg['thread_ts'],  msg['client_msg_id'])
                    msg['thread_replies'] = thread_replies

            # Найдём понятное имя юзера
            if 'user' in msg:
                if msg['user'] not in users:
                    get_user_from_slack(msg['user'])
                    
                msg['user_realname'] = users[msg['user']]

            # Заносим в сообщение человекочитаемое датавремя
            msg_datetime = convert_ts_into_local_timezone(msg['ts'])
            msg['msg_datetime'] = msg_datetime.strftime('%Y.%m.%d %H:%M:%S')

            if do_print:
                msgdate = msg_datetime.date()
                if current_date != msgdate:
                    print(f"Скачали сообщения c {msgdate} по {current_date}...")
                    current_date = msgdate

            # Сохраняем файлы в отдельный список
            if 'files' in msg:
                files.extend(msg['files'])

        messages.extend(j['messages'])

        # Берём курсор для следующих сообщений
        if 'response_metadata' not in j:
            break
        if 'next_cursor' in j['response_metadata']:
            cursor = j['response_metadata']['next_cursor']
        if 'has_more' in j:
            if not j['has_more']:
                break
        else:
            break

    # Сортируем по времени
    messages.sort(key=lambda msg: msg['ts'])
    if do_print:
        print(f'Скачали {msgs_count} сообщений')

    return messages


def download_file(ses, total_count, file_entry):
    global downloaded_file_count

    downloaded_file_count += 1
    # Если файл удалён
    if 'url_private' not in file_entry:
        # downloaded_file_count += 1
        print(f"({downloaded_file_count} / {total_count}) - {file_entry['id']} отсутствует.")
        return False

    fullfilename = ''
    try:
        url = file_entry['url_private']
        filename = file_entry['id'] + splitext(file_entry['url_private'])[1]
        fullfilename = join(files_folder, filename)
    except KeyError as e:
        print(f'у файла нет свойства {e}:')
        print(file_entry)
        return False

    if exists(fullfilename):
        print(f"({downloaded_file_count} / {total_count}) {filename} уже есть в папке")
        # downloaded_file_count += 1
        return True

    try:
        # Качаем файл
        g = ses.get(url)

        with open(fullfilename, 'wb') as f:
            f.write(g.content)
            # downloaded_file_count += 1
            print(f'({downloaded_file_count} / {total_count}) {filename} downloaded.')
        return True

    except Exception as e:
        print(f'Error: {str(e)} ... Filename: {filename}')
        print('Спим минуту...')
        sleep(60)
        return download_file(ses, total_count, file_entry)


def delete_windows_symbols(in_str):
    chars = '\\/|?:*"<>'
    for c in chars:
        in_str = in_str.replace(c, '')
    return in_str


def html_one_message(msg, idx, is_reply=False):
    global users
    global files
    msgclass = 'threaded' if is_reply else 'default'
    html = f'<div class="message {msgclass} clearfix" id="message{idx}">\n' if not is_reply else f'<div class="message {msgclass} clearfix">\n'

    html += (f'<div class="pull_left userpic_wrap">'
             '<div class="userpic userpic5" style="width: 42px; height: 42px">'
             '<div class="initials" style="line-height: 42px">\n'
    )

    # Аватарка
    if 'user' in msg:
        html += f'<img src="users/{msg["user"]}" height=42px width=42px>'

    html += (
        f'</div></div></div>\n<div class="body">\n'
        f'<div class="pull_right date details">{msg["msg_datetime"]}</div>\n'
    )

    if 'user_realname' in msg:
        html += f'<div class="from_name">{msg["user_realname"]}</div>\n'


    # Обрабатываем ссылки
    if 'text' in msg and (msg['text']):
        links = re.findall(r'<.*?>', msg['text'])
        if links:
            for link in links:
                # Обрабатываем упоминание юзера
                if link[1] == "@":
                    user_id = link[2:-1]
                    if user_id not in users:
                        get_user_from_slack(user_id)
                    msg['text'] = msg['text'].replace(link, '@' + users[user_id])
                    continue
                i_pipe = link.find("|")
                if i_pipe != -1:
                    link_url = link[1 : i_pipe]
                    link_text = link[i_pipe+1 : -1]
                    msg['text'] = msg['text'].replace(link, f'<a href="{link_url}">{link_text}</a>')
                else:
                    msg['text'] = msg['text'].replace(link, f'<a href="{link[1:-1]}">{link[1:-1]}</a>')
        
        # Обрабатываем: переводы строк -> <br>, символы табуляции -> на 2 пробела
        msg['text'] = msg['text'].replace('\n', '<br>')
        msg['text'] = msg['text'].replace('\t', '  ')

        # Форматирование (жирный / курсив / зачеркнутый / код / блок кода)
        # На версиях python < 3.7 словарь не сохраняет порядок, поэтому сделал порядок по списку
        d_replaces = {'```': 'pre', '`': 'code', '*': 'b', '_': 'i', '~': 's'}
        for k in ['```', '`', '*', '_', '~']:
            v = d_replaces[k]
            if k in msg['text']:
                msg['text'] = re.sub(rf'(^|\s*|<br>)\{k}(.+?)\{k}(\s|$|<br>)', rf'\1<{v}>\2</{v}>\3', msg['text'])

        # Пробелы, чтобы оставались как в сообщении
        for i_space in range(8, 1, -1):
            msg['text'] = msg['text'].replace(r' ' * i_space, '&nbsp;' * i_space)

        html += f'<div class="text">{msg["text"]}</div>\n'

    # Файлы в сообщении
    if 'files' in msg:
        html += '<div class="clearfix media_wrap">'

        br = '<br>' if len(msg['files']) > 1 else ''

        for file in msg['files']:
            if 'url_private' not in file:
                html += f'&lt;<i>файл удалён</i>&gt;{br}\n'
                continue

            # Добавляем ссылку
            fileext = splitext(file['url_private'])[1]
            html += (f'<a href="{channel_name}_files/{file["id"]}{fileext}" target="_blank">'
                     f'<b>{file["title"]}</b>\n'
            )

            # Добавляем картинку, если файл = изображение
            if file['pretty_type'] in {'PNG', 'JPEG', 'Bitmap', 'GIF'}:
                html += f'<br><img src="{channel_name}_files/{file["id"]}{fileext}" '
                # Правильно выставляем размеры у тега
                if ('original_w' in file) and (int(file['original_w']) / int(file['original_h'])) > 6:
                    html += 'width=100%>\n'
                else:
                    html += 'height=100">\n'

            html += f'</a>{br}'
        html += '</div>'

    # Обсуждение
    if 'thread_replies' in msg:
        for th_idx, th_msg in enumerate(msg['thread_replies']):
            html += html_one_message(th_msg, str(idx) + '_' + str(th_idx), True)

    html += '</div></div>\n'

    return html


def create_html(messages):
    html = f'''<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>{channel_name}</title>
<meta content="width=device-width, initial-scale=1.0" name="viewport"/>
<link href="{files_folder}/style.css" rel="stylesheet"/></head>
<body><div class="page_wrap"><div class="page_header">
<div class="content"><div class="text bold">{channel_name}</div></div></div>
<div class="page_body chat_page"><div class="history">
'''
    for idx, msg in enumerate(messages):
        html += html_one_message(msg, idx)
    html += '</div></div></div></body></html>'
    with open(f'{channel_name}.html', 'w', encoding='utf-8') as htmlfile:
        htmlfile.write(html)


def create_style_css():
    compressed = (b'x\xda\xa5XIs\xdb6\x14>\xc7\xbf\x02\xb5\xa7\x93\xc4#\xca$E-\x96N\x9d\x1c\xdaC\xd3\x1e:\xedU\x03\x12\xa0\x84\x1a\x02X\x00\x94\xecd\xfc\xdf\xfb\xc0'
        b'\x15\xdc,OK\xc5\x13\tx\xf8\xde\xbe\x80\xb1$/\xe8\xfb\r\x82\xe7\x84\xd5\x81\x89-\xf2w\xc5\xcfT\n\xb3EA\x98=?\x04\x9b\xec\x19}\xfc=\xa3\x02\xfd\x81\x85\xfe8\xbb'
        b'\xfd5O\x18\xc1\xe8g\x85\x05\xa1\xb7\xcdo\xbb\x8b\xfe\x14,\x91v\xf5\'\xc50\x9f\xfdB\xf9\x99\x1a\x96\xe0\xd9_T\x11,\xf0L\x03\x95\xa7\xa9b\xe9\xee\xe6\xf5F\x1b%\xc5'
        b'\xa1\x92\xc12\xf5.\x94\x1d\x8e\xc0{\xed\xfb\x96\xc0\x82\xcd\xd0SLf(S\xf0M\xe3S\xe6\x92\xa7\xf8\xc4\xf8\xcb\x16}\xa5\x82\xcb\xd9W)p"g_\xa4\xd0\x92c=\xbb\xfd"s\xc5'
        b'\xa8B\xbf\xd1\xcb\xed\xec$\x85\xd4\x19Nh\r\\\x01e\x98\x10&\x0e[\x04\xea\xa2({nM\xe0i\xf6\x8dn\xd1\xa3\xffc\xb9\x96H.\xd5\x16\xdd%\xebp\x19\xd1r-\xc6\xc9\xd3A\xc9'
        b'\\\x10\xaf\xdeN\x1f\xd30\x8d\xaam\xa9\x08U\x9e\xc2\x84\xe5z[\xc2\xbf\xde\x80.\x15s\xc2t\xc61h\x10s\x99<\xed\xc6\x9c\xc1\x99\xa0\xde\xb1\xb2K0\x8f\xc2\xcdr\x1dD'
        b'\x8br\xf3\x02\xf8^\xac(~\x02\x08\xfb\x9f\x879w\xb6.\ng\xf5\x8e]\xe8*\xb2X,\xa6\xb5X\xda\xcf\xb4\x16v]\x9e\xa9J\xb9\xbcl\x11\xce\x8d\xdcu\xcd\xb9\xa8\xa9\xca\xd3'
        b' ;\xd8\x17\x1c\xc3\x08\xba\xa3\x94\xd6\xba>7\xba\t)\xe8\xc0\xf8L\x1c!Z\x8c\xb5\xda\xc3=;\xd9`\xf9`\x0f]\x181G\xc0\xf4\xads^\xef\x1fn\xe6\xe6\x08Z\x12J\xfan]\x956'
        b'\x9f\'\x9cb\x95\xb2\xe7-N\r\xc4\xc4\xf7\xca\x12\xc2P\x1b\xec\xb7\xe8\xb6\xe4}f\x9a\xc5\x8c3\x03N92B\xa8\xd8Mz\xaa\x96\xbc\xf2T\xc1\x01\x08\xa49\x16\x1c\xb3\x9c'
        b'\xf3=\xa7\xa9\xa9C\x96K\x0c\xd4v\xa5\xddW\x16\xa2KP,\x95\x14\xf8@\xf7\xd6\x87\x15\xc1\x98\x9f\x8a\xa7\xebW\xbfxz\x08\xb8Q\xb9 R\x87\xf8\x93?C\xf0/\\l>\xef\xd0'
        b'\xc3=\xba\x0bV\x1b\x9c\x90\x1d\xba\x7f@\x05\xa9\xa1\xcf\xc6#4\x91\n\x1b&E\xed\xa2.\xec\xf6h\xc3\xa0\x02\x1f\x9c\x00Q\xa9\xb2!<8f\rm\nw}\x18\x17\xa99p\xb4~\xad9d'
        b'\x12\x8e\x15\xd0\xe0KZ\x05\xf47\x8f\x01\x9fg\x1b\x0f\xbb\xf7\x18\xaa\x13=N\x84\x83\xeb\x8c<uCuAWt3\x10f^EN%U\x05\x18m\xfc:\xea\x9b,vr\xa3\x97G>\xfa\x81\x9d2\xa9'
        b'\x0c\x16f\xc0\x00\xf788\x1a)\x9aQ\\$L\xf5u\xa0sk\xa4\x10\x92\x15\x85A\x93\x8b-M\x99_\xe5~U\x97\xe6\xb1\xe4\xa4\x1b&wa`?\xbb\xc9\x12=\'\xd4`\xc6u\xef\xd8\xda_\xaf'
        b'\xd7\xf1\xb4\xd9\xe66R\x06\x15\xb8\x16\x06\x85a#V\xaf \xd8\x9d~\xf5q\xf3\xb4\x88\xc0v\x8br\xce2\xcdt\xe5\xf7#\x84\x9cWt\x01k>\x1b\x89o\x18\xfe=2n\xc2\xcat\x05D'
        b'\xdc\xb6\xd4\xea\x80g$\x14\xe0U\xa3\xc9\xf5@\xa9\xb1p,\xf3i\xee\x05\xdd\x85\x99\xe3\x9e\xb03k\xd3\xa3\n\xb1\x82\xebx\x14\xe7\xd0~3\x96\xec!)\x9f\xde\xe8B\xd3\xb9'
        b'\xef\x02\xbc\x9d\xfc\xfd#o\xb0\xeb\xa5\xc6\xb2N\xcc\xa1\x93\x1d\xb89\x13\x10\xe6\xb8\t\xbe1`\'\xfd\x1d\xc50g\x07\x100\x017S\xe5\xac\x1b\x98jt*\x15\xd4\x80<\xcb'
        b'\xa8J\xb0\xaez\x92\xe5\t\x93\x0b\xa7\x89q\x14+\xd0\xf7\x8a\x92Y#U\x00_O\x940\xbcO\xa0\x17\xa3y\xca8o\x96\xe0\x07\xed-qv\xa6{\x90\xb7\xb0Y\xb9\xf7V\xb5_\xc2\xe3'
        b'\xb0>(JE\xcb<\xec0\x9f\xeb<I\xa8\xd6=\x8e\xd9Q\x1ay\x8d\xd3*\x8a\xd3h\xedpz\x81T\x92\x97\x96\xd5\xa2\xc1;S\x91\xd3\xeb\x92\xe3\xb8\xaa\x18%^\xccs\xda\xa2E\r\x1a'
        b'\xce\t\x93c\x86:K\x96\xd0\xfd\t\xf4\x81\xf4\xb8\xc6.J\x1f\x13\xf2\xe8\xb0\xcbr\x95q\x87\xe1\xb2\xc1=\xe0\xd3U\xb8\xc7\xcd&\xaa\xf2\xa7\x82\x83\xe0o\xc1V\r\x18'
        b'\x13\x85\x98\xd7\xf0\xe8j\x1d`\xd7\x8f\x9a\xe2\x16n\xdd\x06G\'.ZS@\xc6_\xf5`\xb4\x8e\x13\x128< /\xc5\xc11\xc1\xa6\r\x16\xa8x81\xd7}\xb8I\xa2\xa8,QTi\x18\xb79('
        b'\x9c\xca\xb1*5B5\x99\xb7Ny_\xf8\xe3G\x95\xbc\xe8\xb11\xca-\xb5\xaalLS\xdc\x05\xf8Yw\xfbu\xb0\x9a\xa0u\xb4\xaaI\xa7`c\xd6\xa3\x8c\xfci\x1dz\xcd\xa1\x997\xaa!\x15'
        b'\xcf\x8b\xca\xf5\x9fjsg\x98\x98\xbe{\xb8,\xaeW\xef!\xe8\xe8Ma\x9d\x96\xb9\xa1\xa1:\xc2Y\xdd\x0f\x88\xc0\xb6"\xdf%\xa9(\xea\t:\xda\x8cL(\xbd)&\x08Gh\xae\xcfBcsN-'
        b'\x04$F.\xda\xdb@w\xfe\xeej\xd0\xb4}\x7f0\x92\x04\xcb>,\xc71\xe5\x03#,\xedi\xf8l\xc2\x91\xb9\xa6\x01\xe1L\x9b}V\x94\xb8\xe99\xc0\x06\xcc\xa48\xc1\x10\t\xfa\x9cz'
        b'\xd9\xdb\x85Q$\x7f\x94~@\xea[/\xac&\xd0\xdf\x95\xde\xc1f\xf2t\xdc\x7f\x1d\xe1\xd9\x0c\x87\xd9i\x9a\xa1\xcd\xe8\xbe\x90\x85M\xd0\xa8\x85\xa3I \x9d\xc7C\xacr\x88'
        b'\x9a>T\r\xbe\xfb\x11[];\xea\x94\x97\x81\xe3\x060u`\x1c\x01F\x8ex\xa5u`\xdd\x1b{/u\x90\x174\xd3f1\xe0TY\xd5\xcfe\x14\xce}\x8dh1\xf2\xbc\xde\xc0\\9/\xe7\x9d\xe6N'
        b'=L~\xb8\xb2\xe1Oa\x14\xce\xc2h5\x0b\x97>\xfc-?\xbf\x83\x93?_\xb6\x9c\x80\x8d:\xdb\xa69\x16oN\xce\x96Dn\xa0\x8c\xcdr.i=\xaa:W\xe8\x8ei\x03\xbf\x0f\xde\x9dTGf\xf3'
        b'\x01\xe5\x1b\xd1\xde\xc8\xde\x0c-\xefI\x91:\xde\tMq\xce\xcd\x98U\xdc\xfd\xf9\xdf\x12\xae\xd7\xa4\x9b:\x85v^\x9f\x14\xfa\xbb\x92\xa7\xbd\x13\xea\xcd\x9b\xa0\xcd'
        b'cH\xe2\xa9[\xdeX\xcbZNC\xf7\xae\x84\x1d<\x01\xb35\xe6\xdd\x93\x93\x99?\x10\xdf\xb9\x90\xbd\xf1z\xab\xfb\xc6\xac\xb9J\xe4\xe5\xcbI/f\x84m\x114U&,\xde\xf0V\xa0\rV'
        b'\xa6\xcb\x18z\x0b\x7f\xd9\x1b9s\xd6\xca\xd1i$\xb4&MT\x9cx+;\x1b\'\xdbV1\xf4^y\xbe\x1e\x05\xfb\xcb\xe6\x98\x9f\xe2\xfe\xfb\x88\x1ay\xa4\xd1\x8e\xdc\xb8&\x98\xfd'
        b'\xbfW\x10\xb6o\x8f7\xef\xb1\xd6<\xd0\x8a\x99bl\xef\x99=\x93\xf6n\xf5ON\xb53M\x0c\xab\xefT\x0b\xe8s!T\'\x8ae\x0eV\xff]\xda\xfb\xf0\x17\x13\xf8\x10R&\xd7\xef\x16s'
        b'\x00S\x0c\xfc\xc5\xa5\xa8\x088\xd7\x1eX\xb0\x13\x86\x1a\xdd\x89\xc4\xc6\xf8\x8ar\x18\xe7\xcet\nn\x0c\xc9]+\xee\x8a\xee\x02\xd8;yj&\xa6\xfet\xfaz\xf3/ \xd0\xa9~'
    )
    decompressed = zlib.decompress(compressed)
    with open(join(files_folder, 'style.css'), 'w', encoding='utf-8') as style_file:
        style_file.write(decompressed.decode('utf-8'))

# ------------------------------------------------------------------------------

chdir(dirname(__file__))
files_folder = ''

downloaded_file_count = 0

ses_users = req.Session()
ses = req.Session()
ses.headers.update({
    "Content-Type": "application/x-www-form-urlencoded",
    "Authorization": f"Bearer {TOKEN}"
})
t1 = time()

# Получаем имя канала
while True:
    try:
        CHANNEL = input('Скопируйте и введите ID нужного канала (вида C01EQ5V9665), но '
                        'сначала проверьте, добавлено ли приложение в этот канал\nID: ').strip()
        # junior chat = C02HT9EALVB
        # support chat = C02EQ5V9665

        j = http_get(ses, 'conversations.info', {'channel': CHANNEL})
        break
    except ChannelNotFoundException as e:
        print(f'Error: Канал с ID {CHANNEL} не найден')
    except OtherException as e:
        print(e)

try:
    channel_name = delete_windows_symbols(j['channel']['name'])
    print(f'Скачиваем канал {channel_name}')
    files_folder = f'{channel_name}_files'

    # Скачиваем историю чата
    params = {
        'channel': CHANNEL,
        'limit': MESSAGES_PER_REQUEST
    }
    messages = get_messages(ses, 'conversations.history', params)
    t2 = time()
    print('Завершено за {:.2f} секунд'.format(t2-t1))

    ### DEBUG
    # Сохранение json с сообщениями
    # with open(f'{channel_name}.json', 'w', encoding='utf-8') as f:
    #     json.dump(messages, f, indent=4, ensure_ascii=False)

    ### DEBUG
    # Подгружаем файлы из уже скачанного файла сообщений
    # with open(f'{channel_name}.json', 'r', encoding='utf-8') as fil:
    #     j = json.load(fil)
    # msg_count = 0
    # files_count = 0
    # makedirs(files_folder, exist_ok=True)
    # for msg in j:
    #     if 'files' in msg:
    #         msg_count += 1
    #         files_count += len(msg['files'])
    #         files.extend(msg['files'])  
    #         if 'thread_replies' in msg:
    #             for replmsg in msg['thread_replies']:
    #                 if 'files' in replmsg:
    #                     files.extend(replmsg['files'])
    #                     files_count += 1
    # print(msg_count, files_count, len(files)) 
    # --------------------------------------------------------------------------

    # Скачивание файлов-прикреплений из сообщений
    print()
    while True:
        # do_download_files = input('Скачать файлы-прикрепления из сообщений? (y/n) (д/н): > ')
        do_download_files = 'y'
        if len(do_download_files.strip()) == 1:
            if do_download_files in 'yYдДnNнН':
                break
        print('Непонятный ответ :)')

    if do_download_files in 'yYдД':
        print('Скачиваем файлы...')
        makedirs(files_folder, exist_ok=True)
        threads = 4
        pool = ThreadPool(threads)
        pool.map(partial(download_file, ses, len(files)), files)
        pool.close()
        pool.join()
        print(f'Файлы-прикрепления сохранены в папку {files_folder}')
    # print(f'История чата сохранена в файл {channel_name}.json')

    create_html(messages)
    create_style_css()

    print(f'История чата сохранена в файл {channel_name}.html')
    if exists(f'{channel_name}.html'):
        Popen(('start', f'{channel_name}.html'), shell=True)
    print('Завершено')

except Exception as e:
    raise e
    # print(e)
finally:
    ses.close()
    ses_users.close()

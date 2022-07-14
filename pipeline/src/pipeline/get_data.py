import tweepy
import facebook
import pandas as pd
import requests
import os
from google.oauth2 import service_account
import googleapiclient.discovery
from telethon import TelegramClient, events, sync
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetFullChannelRequest
from pipeline.utils import get_blob_service_client, get_secret_keyvault, arrange_telegram_messages, save_data
import logging
import datetime

# -*- coding: utf-8 -*-
try:
    import json
except ImportError:
    import simplejson as json


def get_twitter(config):
    logging.info('getting twitter data')

    # initialize twitter API
    twitter_secrets = get_secret_keyvault("twitter-secret", config)
    twitter_secrets = json.loads(twitter_secrets)
    auth = tweepy.OAuthHandler(twitter_secrets['CONSUMER_KEY'], twitter_secrets['CONSUMER_SECRET'])
    auth.set_access_token(twitter_secrets['ACCESS_TOKEN'], twitter_secrets['ACCESS_SECRET'])
    api = tweepy.API(auth, wait_on_rate_limit=True, wait_on_rate_limit_notify=True, compression=True)

    twitter_data_path = "./twitter"
    os.makedirs(twitter_data_path, exist_ok=True)

    # track individual twitter users
    if config["track-twitter-users"]:
        df_twitter_users_to_track = pd.read_csv('../config/tweets_to_track.csv')
        tw_users = df_twitter_users_to_track.dropna()['user_id'].tolist()
        if len(tw_users) == 0:
            raise ValueError("No twitter user specified")

        for userID in tw_users:
            # save output as
            save_file = twitter_data_path + '/tweets_' + userID + '.json'

            tweets = api.user_timeline(screen_name=userID,
                                       count=200,
                                       include_rts=False,
                                       tweet_mode='extended'
                                       )

            all_tweets = []
            all_tweets.extend(tweets)
            oldest_id = tweets[-1].id
            while True:
                tweets = api.user_timeline(screen_name=userID,
                                           count=200,
                                           include_rts=False,
                                           max_id=oldest_id - 1,
                                           tweet_mode='extended'
                                           )
                if len(tweets) == 0:
                    break
                oldest_id = tweets[-1].id
                all_tweets.extend(tweets)

            with open(save_file, 'a') as tf:
                for tweet in all_tweets:
                    try:
                        tf.write('\n')
                        json.dump(tweet._json, tf)
                    except Exception as e:
                        logging.warning("Some error occurred, skipping tweet:")
                        logging.warning(e)
                        pass

    # track specific queries
    if config["track-twitter-queries"]:
        save_file = twitter_data_path + '/tweets_queries.json'
        queries = config["twitter-queries"]
        if len(queries) == 0:
            raise ValueError("No twitter query specified")
        all_tweets = []
        # loop over queries and search
        for query in queries:
            n = 0
            try:
                for page in tweepy.Cursor(api.search,
                                          q=query,
                                          tweet_mode='extended',
                                          include_entities=True,
                                          max_results=100).pages():
                    # logging.info('processing page {0}'.format(n))
                    try:
                        for tweet in page:
                            all_tweets.append(tweet)
                    except Exception as e:
                        logging.warning("Some error occurred, skipping page {0}:".format(n))
                        logging.warning(e)
                        pass
                    n += 1
            except Exception as e:
                logging.warning("Some error occurred, skipping query {0}:".format(query))
                logging.warning(e)
                pass

        with open(save_file, 'a') as tf:
            for tweet in all_tweets:
                try:
                    tf.write('\n')
                    json.dump(tweet._json, tf)
                except Exception as e:
                    logging.warning("Some error occurred, skipping tweet:")
                    logging.warning(e)
                    pass


    # parse tweets and store in dataframe
    df_tweets = pd.DataFrame()
    for file in os.listdir(twitter_data_path):
        if file.endswith('.json'):
            df_tweets_ = pd.read_json(os.path.join(twitter_data_path, file), lines=True)
            df_tweets = df_tweets.append(df_tweets_, ignore_index=True)
    # drop duplicates
    df_tweets = df_tweets.drop_duplicates(subset=['id'])

    save_data("tweets", "twitter", df_tweets, "id", config)


def get_youtube(config):

    df_youtube_channels_to_track = pd.read_csv('../config/youtube_to_track.csv')
    channel_ids = df_youtube_channels_to_track.dropna()['channel_id'].tolist()
    if len(channel_ids) == 0:
        raise ValueError("No youtube channel specified")

    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1" # Disable OAuthlib's HTTPS verification
    api_service_name = "youtube"
    api_version = "v3"
    service_account_info = get_secret_keyvault('google-secret', config)
    credentials = service_account.Credentials.from_service_account_info(json.loads(service_account_info))
    youtube = googleapiclient.discovery.build(
        api_service_name, api_version, credentials=credentials)

    df_videos = pd.DataFrame()

    for channel_id in channel_ids:
        request = youtube.search().list(
            part="snippet,id",
            maxResults=50,
            order='date',
            channelId=channel_id,
            type='video'
        )
        response = request.execute()
        for item in response['items']:
            title = item['snippet']['title']
            description = item['snippet']['description']
            videoId = item['id']['videoId']
            request = youtube.videos().list(
                part="snippet,contentDetails,statistics",
                id=videoId
            )
            response = request.execute()['items'][0]
            if 'viewCount' in response['statistics'].keys():
                viewCount = response['statistics']['viewCount']
            else:
                viewCount = None
            if 'likeCount' in response['statistics'].keys():
                likeCount = response['statistics']['likeCount']
            else:
                likeCount = None
            if 'dislikeCount' in response['statistics'].keys():
                dislikeCount = response['statistics']['dislikeCount']
            else:
                dislikeCount = None
            if 'commentCount' in response['statistics'].keys():
                commentCount = response['statistics']['commentCount']
            else:
                commentCount = None
            publishedAt = response['snippet']['publishedAt']
            source = response['snippet']['channelTitle']
            url = f"https://www.youtube.com/watch?v={videoId}"
            df_videos = df_videos.append(pd.Series({
                'full_text': title,
                'description': description,
                'id': videoId,
                'source': source,
                'viewCount': viewCount,
                'likeCount': likeCount,
                'dislikeCount': dislikeCount,
                'commentCount': commentCount,
                'created_at': publishedAt,
                'url': url,
                'lang': 'unknown'
            }), ignore_index=True)

    save_data("videos", "youtube", df_videos, "id", config)


def get_kobo(config):
    # get data from kobo
    kobo_secrets = get_secret_keyvault('kobo-secret', config)
    kobo_secrets = json.loads(kobo_secrets)
    headers = {'Authorization': f'Token {kobo_secrets["token"]}'}
    data_request = requests.get(f'https://kobonew.ifrc.org/api/v2/assets/{kobo_secrets["asset"]}/data.json',
                                headers=headers)
    data = data_request.json()['results']
    df_form = pd.DataFrame(data)

    save_data("form_data", "kobo", df_form, "_id", config)


def get_facebook(config):

    # get data from facebook
    # facebook_secrets = get_secret_keyvault('facebook-secret', config)
    # facebook_secrets = json.loads(facebook_secrets)
    facebook_secrets = {"token": "EAAHZCG5YvZCdkBAGcRZAYPodE27mtEp8ubp6ZAVLLCm83A9FpUKveFT1afJh2nBZC4ZAp3qRVNjEQhpZC08ITFAcqFX9LWtlGTPY56ZAQp1LMjhZBuRuc7YQSTQEjwKwmAlAR9Tff9p5QvvfnPiu3xZCaCRqHSrjBfJpKgfQtnUmwYexYZACJZBFsmd5UtZB0ZBlxTlNwIypSienkNZBeC0xIZBSDES9", \
    "page": "612726123570520"}# "387184371438297"}
    graph = facebook.GraphAPI(
        access_token=facebook_secrets["token"])#,
        #version="3.1")
    reaction_types = ['LIKE', 'LOVE', 'WOW', 'HAHA', 'SORRY', 'ANGRY', \
        'THANKFUL', 'PRIDE', 'CARE', 'FIRE', 'HUNDRED']

    # get all comments to posts
    df_posts = pd.DataFrame()
    df_comments = pd.DataFrame()

    page_posts = graph.get_object(id=facebook_secrets["page"], fields="feed")['feed']
    # print(page_posts)
    while True:
        for post in page_posts['data']:
            # print(post)
            stats_to_save = {'id_post': post["id"]}
            stats_to_save['source'] = facebook_secrets["page"]
            # stats_to_save['id_comment'] = 0
            stats_to_save['datetime'] =  post['updated_time']
            stats = graph.get_object(id=post["id"], fields="message,shares,reactions.summary(true)")
            # print(stats)
            if 'message' in stats.keys():
                stats_to_save['text_post'] = stats['message']
            # if 'shares' in stats.keys():
            #     stats_to_save['shares'] = stats['shares']['count']
            # if 'reactions' in stats.keys():
            #     stats_to_save['reaction_count'] = stats['reactions']['summary']['total_count']
            #     if stats['reactions']['summary']['viewer_reaction'] in reaction_types:
            #         for reaction in [stats['reactions']['summary']['viewer_reaction']]:
            #             stats_to_save[reaction.lower()] = stats['reactions']['summary']['total_count']
            # better, use reactions.type(TYPE).summary(total_count) for TYPE=LIKE, LOVE, WOW, HAHA, SORRY, ANGRY
            # print(stats_to_save)
            stats_to_save['post'] = True
            df_posts = df_posts.append(pd.Series(stats_to_save), ignore_index=True)

            comments = {'data': []}
            try:
                comments = graph.get_object(id=post["id"], fields="comments")['comments']
            except KeyError:
                continue

            while True:
                for comment in comments['data']:
                    # print(comment)
                    stats = graph.get_object(id=comment["id"], fields="message,from,reactions.summary(true)")#like_count")
                    # print(stats)
                    stats_to_save = {'id_post': post["id"]}
                    stats_to_save['source'] = facebook_secrets["page"]
                    # stats_to_save['id_comment'] = stats['id']
                    stats_to_save['datetime'] = comment['created_time']
                    stats_to_save['text_post'] = stats['text']
                    stats_to_save['text_reply'] = None
                    stats_to_save['post'] = False
                    # stats_to_save['reaction_count'] = stats['reactions']['summary']['total_count']
                    # if stats['reactions']['summary']['viewer_reaction'] in reaction_types:
                    #     for reaction in [stats['reactions']['summary']['viewer_reaction']]:
                    #         stats_to_save[reaction.lower()] = stats['reactions']['summary']['total_count']
                    # print(stats_to_save)
                    df_comments = df_comments.append(pd.Series(stats_to_save), ignore_index=True)

                    while True:
                        for comment in comments['data']:
                            # print(comment)
                            stats = graph.get_object(id=comment["id"], fields="message,from,reactions.summary(true)")#like_count")
                            # print(stats)
                            stats_to_save = {'id_post': post["id"]}
                            stats_to_save['source'] = facebook_secrets["page"]
                            # stats_to_save['id_comment'] = stats['id']
                            stats_to_save['datetime'] = comment['created_time']
                            stats_to_save['text'] = stats['text']
                            stats_to_save['post'] = False
                            # stats_to_save['reaction_count'] = stats['reactions']['summary']['total_count']
                            # if stats['reactions']['summary']['viewer_reaction'] in reaction_types:
                            #     for reaction in [stats['reactions']['summary']['viewer_reaction']]:
                            #         stats_to_save[reaction.lower()] = stats['reactions']['summary']['total_count']
                            # print(stats_to_save)
                            df_comments = df_comments.append(pd.Series(stats_to_save), ignore_index=True)
                            
                            try:
                                comments = requests.get(comments["paging"]["next"]).json()
                                # print(comments)
                            except KeyError:
                                break

                try:
                    comments = requests.get(comments["paging"]["next"]).json()
                    # print(comments)
                except KeyError:
                    break
            
            df_posts = df_posts.append(df_comments)
            

        try:
            page_posts = requests.get(page_posts["paging"]["next"]).json()
        except KeyError:
            break
    
    df_posts.reset_index(inplace=True)
    df_posts['id'] = df_posts.index

    save_data("facebook_posts", "facebook", df_posts, "id", config)
    # save_data("facebook_comments", "facebook", df_comments, "id_comment", config)


def get_telegram(config):

    logging.info("Getting telegram data")

    #get data from telegram
    telegram_secrets = get_secret_keyvault("telegram-secret", config)
    telegram_secrets = json.loads(telegram_secrets)

    telegram_client = TelegramClient(
        StringSession(telegram_secrets["session-string"]),#"rou-smm-bot",
        telegram_secrets["api-id"],
        telegram_secrets["api-hash"]
    )
    telegram_client.connect()  # start(bot_token=telegram_secrets["bot-token"])
    logging.info("Telegram client connected")

    telegram_channels = config["telegram-channels"]
    end_date = datetime.datetime.today().date()
    start_date = end_date - pd.Timedelta(days=14)

    df_messages = pd.DataFrame()
    df_member_counts = pd.DataFrame()
    for channel in telegram_channels:
        try:
            channel_entity = telegram_client.get_entity(channel)
            channel_full_info = telegram_client(GetFullChannelRequest(channel=channel_entity))

            for message in telegram_client.iter_messages(
                channel_entity,
                offset_date=start_date,
                reverse=True
            ):
                reply = None
                df_messages = arrange_telegram_messages(df_messages, message, reply, channel)
                if message.replies:
                    df_replies = pd.DataFrame()
                    for reply in telegram_client.iter_messages(
                        channel_entity,
                        reply_to=message.id
                    ):
                        df_replies = arrange_telegram_messages(df_replies, message, reply, channel)
                    df_messages = df_messages.append(df_replies, ignore_index=True)

            idx = len(df_member_counts)
            df_member_counts.at[idx, 'source'] = channel
            member_count = channel_full_info.full_chat.participants_count
            df_member_counts.at[idx, 'member_count'] = member_count
            df_member_counts.at[idx, 'date'] = end_date
            df_member_counts.at[idx, 'message_count'] = len(df_messages[df_messages['source']==channel])
        except Exception as e:
            logging.error(f"in getting in telegram channel {channel}: {e}")

    # Add index column
    df_member_counts.reset_index(inplace=True)
    df_member_counts['id'] = df_member_counts.index
    df_messages.reset_index(inplace=True)
    df_messages['id'] = df_messages.index
    df_messages['date'] = pd.to_datetime(df_messages['datetime']).dt.strftime('%Y-%m-%d')

    logging.info("Saving Telegram data")
    save_data(f"{config['country-code']}_TL_messages_{start_date}_{end_date}",
              "telegram",
              df_messages,
              "id",
              config)
    save_data(f"{config['country-code']}_TL_membercount_{start_date}_{end_date}",
              "telegram",
              df_member_counts,
              "id",
              config)

    telegram_client.disconnect()
    logging.info("Telegram client disconnected")

import random
from datetime import timedelta
from typing import Optional

import numpy as np
from sqlalchemy import func
from sqlalchemy import or_, desc, not_
from sqlalchemy.orm import aliased
from sqlalchemy.sql.functions import coalesce, sum

from capitalization_model import CapitalizationModelScheduler, CapitalizationMode
from messages import *
from ml_common import create_spacy_instance
from pos_tree_model import rebuild_pos_tree_from_db


from reaction_model import AOLReactionModelScheduler


class BotReply(object):
    def __init__(self, timestamp=None, tokens: dict = [], fresh=False):
        self.timestamp = timestamp
        self.tokens = tokens
        self.fresh = fresh


class BotReplyTracker(object):
    def __init__(self):
        self.replies = {}

    # Creates relevant nodes in reply tracker for server and channel
    def branch_(self, args: MessageArguments) -> None:
        try:
            self.replies[args.server_id]
        except KeyError:
            self.replies[args.server_id] = {}

        try:
            self.replies[args.server_id][args.channel_str]
        except KeyError:
            self.replies[args.server_id][args.channel_str] = BotReply()

    # Called whenever the bot sends a message in a channel
    def bot_reply(self, output_message: MessageOutput) -> None:
        self.branch_(output_message.args)
        self.replies[output_message.args.server_id][output_message.args.channel_str] = BotReply(
            tokens=output_message.tokens, timestamp=output_message.args.timestamp, fresh=True)

    # Called whenever a human sends a message to a channel
    def human_reply(self, input_message: MessageInput) -> None:
        self.get_reply(input_message).fresh = False

    # Gets the last bot reply in a channel
    def get_reply(self, input_message: MessageInput) -> BotReply:
        self.branch_(input_message.args)
        return self.replies[input_message.args.server_id][input_message.args.channel_str]


class MarkovAI(object):
    def __init__(self, rebuild_pos_tree: bool = False):
        print("MarkovAI __init__")
        self.rebuilding = False
        print("MarkovAI __init__: Loading ML models...")
        self.reaction_model = AOLReactionModelScheduler(path=CONFIG_MARKOV_REACTION_PREDICT_MODEL_PATH, use_gpu=CONFIG_USE_GPU)
        self.reaction_model.start()
        self.capitalization_model = CapitalizationModelScheduler(path=CONFIG_CAPITALIZATION_MODEL_PATH, use_gpu=CONFIG_USE_GPU)
        self.capitalization_model.start()
        self.reply_tracker = BotReplyTracker()
        print("MarkovAI __init__: Loading NLP DB...")
        self.nlp = create_spacy_instance()
        print("MarkovAI __init__: Loading PoS tree...")
        if rebuild_pos_tree:
            rebuild_pos_tree_from_db(self.nlp)
        self.pos_tree_model = PosTreeModel(path=CONFIG_POS_TREE_PATH, nlp=self.nlp)
        self.session = Session()
        print("MarkovAI __init__")

    def rebuild_db(self, ignore: Optional[list] = None, author: Optional[list] = None) -> None:

        if ignore is None:
            ignore = []
        if self.rebuilding:
            return

        print("Rebuilding DB...")

        self.rebuilding = True

        if CONFIG_DATABASE == CONFIG_DATABASE_SQLITE:
            self.session.execute("VACUUM")

        self.session.query(URL).delete()
        self.session.query(WordRelation).delete()
        self.session.query(WordNeighbor).delete()
        self.session.query(Word).delete()
        self.session.query(Pos).delete()

        self.session.commit()

        if author is None:
            lines = self.session.query(Line).filter(or_(not_(Line.channel.in_(ignore)), Line.channel == None)).order_by(
                Line.timestamp.asc()).all()
        else:
            lines = self.session.query(Line).filter(
                and_(or_(not_(Line.channel.in_(ignore)), Line.channel == None), Line.author.in_(author))).order_by(
                Line.timestamp.asc()).all()

        for line_idx, line in enumerate(lines):

            input_message = MessageInput(line=line)
            input_message.load(session=self.session, nlp=self.nlp)

            progress = (line_idx / float(len(lines))) * 100
            print("%f%%: %s" % (progress, input_message.message_filtered))

            if line.server_id == 0:
                continue

            self.process_msg(None, input_message, rebuild_db=True)

            self.rebuilding = False

        if CONFIG_DATABASE == CONFIG_DATABASE_SQLITE:
            self.session.execute("VACUUM")
        elif CONFIG_DATABASE == CONFIG_DATABASE_MYSQL:
            self.session.execute("OPTIMIZE TABLE WORD")
            self.session.execute("OPTIMIZE TABLE POS")
            self.session.execute("OPTIMIZE TABLE WORDRELATION")
            self.session.execute("OPTIMIZE TABLE WORDNEIGHBOR")
            self.session.execute("OPTIMIZE TABLE URL")

        print("Rebuilding DB Complete!")

    def learn(self, input_message: MessageInput) -> None:
        for token_index, token in enumerate(input_message.tokens):

            # Uprate Words
            token['word'].count += 1
            token['word'].rating += 1

            # Uprate Word Relations
            if token_index < len(input_message.tokens) - 1 and 'word_a->b' in token:
                token['word_a->b'].count += 1
                token['word_a->b'].rating += 1

            # Uprate POS
            token['pos'].count += 1

            # Uprate Word Neighbors
            for neighbor in token['word_neighbors']:
                neighbor.count += 1
                neighbor.rating += 1

        self.session.commit()

    def cmd_stats(self) -> str:
        words = self.session.query(Word.id).count()

        if CONFIG_DISCORD_MINI_ME is not None:
            lines = self.session.query(Line.id).filter(Line.author.in_(CONFIG_DISCORD_MINI_ME)).count()
        else:
            lines = self.session.query(Line.id).filter(Line.author != CONFIG_DISCORD_ME).count()
        assoc = self.session.query(WordRelation).count()
        neigh = self.session.query(WordNeighbor).count()
        return "I know %d words (%d associations, %8.2f per word, %d neighbors, %8.2f per word), %d lines." % (
            words, assoc, float(assoc) / float(words), neigh, float(neigh) / float(words), lines)

    def command(self, command_message: MessageInputCommand) -> str:
        result = None

        if command_message.message_raw.startswith(CONFIG_COMMAND_TOKEN + "stats"):
            result = self.cmd_stats()

        if command_message.message_raw.startswith(CONFIG_COMMAND_TOKEN + "essay"):
            result = self.essay(command_message)

        return result

    def essay(self, command_message: MessageInputCommand) -> str:
        command_message.load(self.session, self.nlp)

        txt = ""

        for p in range(0, 5):

            # Lead In
            reply = self.reply(command_message, no_url=True)
            if reply is None:
                txt = "I don't know that word well enough!"
                break
            txt += "\t" + reply + " "

            # Body sentences
            for i in range(0, 3):

                feedback_reply_output = MessageOutput(text=reply)
                feedback_reply_output.args.author_mention = command_message.args.author_mention

                feedback_reply_output.load(self.session, self.nlp)

                # noinspection PyTypeChecker
                reply = self.reply(feedback_reply_output, no_url=True)
                if reply is None:
                    txt = "I don't know that word well enough!"
                    break
                txt += reply + " "

            reply = self.reply(command_message, no_url=True)

            # Lead Out
            if reply is None:
                txt = "I don't know that word well enough!"
                break
            txt += reply + " "
            txt += "\n"

        return txt

    def reply(self, input_message: MessageInput, no_url=False) -> Optional[str]:
        selected_topics = []
        potential_topics = input_message.tokens

        # If we are mentioned, we don't want things to be about us
        if input_message.args.mentioned:
            potential_topics = [x for x in input_message.tokens if
                                x['word'].text.lower() != CONFIG_DISCORD_ME_SHORT.lower()]

        potential_subject = None

        # TODO: Fix hack
        if type(input_message) == MessageInputCommand:
            potential_topics = [x for x in potential_topics if
                                "essay" not in x['word'].text]

        for word in potential_topics:

            potential_subject_pos = word['pos'].text

            if potential_subject_pos in CONFIG_MARKOV_TOPIC_SELECTION_POS:
                selected_topics.append(word)

        # Fallback to without PoS filter
        if len(selected_topics) == 0:
            selected_topics = potential_topics

        # Fallback to any word in sentence
        if len(selected_topics) == 0:
            selected_topics = input_message.tokens

        selected_topic_id = []
        selected_topic_text = []

        for topic in selected_topics:
            selected_topic_id.append(topic['word'].id)
            selected_topic_text.append(topic['word'].text)

        # Find potential exact matches, weigh by occurance
        subject_words = self.session.query(Word.id, Word.text, Word.pos_id, Pos.text.label('pos_text'),
                                           Word.count.label('rating')).filter(
            and_(Word.id.in_(selected_topic_id), Word.pos_id == Pos.id)).order_by(desc('rating')).all()

        if CONFIG_MARKOV_DEBUG:
            print("Subject Words: %s" % (str(subject_words)))

        if len(subject_words) > 1:
            # Linear distribution to choose word
            potential_subject = subject_words[int(np.random.triangular(0.0, 0.0, 1.0) * len(subject_words))]
        elif len(subject_words) == 1:
            potential_subject = subject_words[0]

        if potential_subject is None:
            if CONFIG_MARKOV_DEBUG:
                print("Subject Fallback!")
            subject_word = self.session.query(Word.id, Word.text, Word.pos_id, Pos.text.label('pos_text')).join(Pos,
                                                                                                                Pos.id == Word.pos_id).filter(
                Word.text == CONFIG_DISCORD_ME_SHORT).first()
        else:
            subject_word = potential_subject

        last_word = subject_word

        if CONFIG_MARKOV_DEBUG:
            print("Subject: %s Pos: %s" % (potential_subject.text, potential_subject.pos_text))

        topic_me = False
        if subject_word.text.lower() == CONFIG_DISCORD_ME_SHORT.lower():
            topic_me = True

        # TODO: Optimize this
        # Give preference to the POS we are looking for when generating the
        # sentence structure in PosTreeModel.generate_sentence
        loops = 0
        sentence_structure = []
        while potential_subject.pos_text not in sentence_structure and loops < 100:
            sentence_structure = self.pos_tree_model.generate_sentence(words=[])
            loops += 1

        pos_index = None

        for p_idx, pos in enumerate(sentence_structure):
            if pos == potential_subject.pos_text:
                pos_index = p_idx
                break

        if pos_index is None:
            return None

        if CONFIG_MARKOV_DEBUG:
            print("Sentence Structure: %s" % str(sentence_structure))

        pos_index_forward = pos_index
        pos_index_backward = pos_index

        if len(sentence_structure) != 1:
            forward_count = len(sentence_structure) - (pos_index + 1)
            backward_count = pos_index + 1
        else:
            forward_count = 0
            backward_count = 0

        # Generate Backwards
        backwards_words = []
        f_id = subject_word.id
        count = 0
        while count < backward_count:
            pos_index_backward -= 1
            choice = sentence_structure[pos_index_backward]

            # Most Intelligent search for next word (neighbor and pos)
            word_a = aliased(Word)
            word_b = aliased(Word)

            query = self.session.query(word_a.id, word_a.text, word_a.pos_id,
                                       (coalesce(sum(word_b.count), 0) * CONFIG_MARKOV_WEIGHT_WORDCOUNT
                                        + coalesce(sum(WordNeighbor.rating), 0) * CONFIG_MARKOV_WEIGHT_NEIGHBOR
                                        + coalesce(sum(WordRelation.rating),
                                                   0) * CONFIG_MARKOV_WEIGHT_RELATION).label(
                                           'rating')). \
                join(word_b, word_b.id == f_id). \
                join(Pos, Pos.id == word_a.pos_id). \
                outerjoin(WordRelation, and_(WordRelation.a_id == word_a.id, WordRelation.b_id == word_b.id)). \
                outerjoin(WordNeighbor, and_(word_a.id == WordNeighbor.b_id, WordNeighbor.a_id == subject_word.id)). \
                filter(Pos.text == choice). \
                filter(or_(WordNeighbor.rating > 0, WordNeighbor == None)). \
                filter(or_(WordRelation.rating > 0, WordRelation == None)). \
                filter(not_(and_(WordNeighbor == None, WordRelation == None)))

            if not topic_me:
                query = query.filter(word_a.text != CONFIG_DISCORD_ME_SHORT)

            query = query.group_by(word_a.id). \
                order_by(desc('rating')). \
                limit(CONFIG_MARKOV_GENERATE_LIMIT)

            results = query.all()

            if len(results) == 0:

                query = self.session.query(word_a.id, word_a.text, word_a.pos_id,
                                           (coalesce(sum(word_b.count), 0) * CONFIG_MARKOV_WEIGHT_WORDCOUNT
                                            + coalesce(sum(WordNeighbor.rating), 0) * CONFIG_MARKOV_WEIGHT_NEIGHBOR
                                            + coalesce(sum(WordRelation.rating),
                                                       0) * CONFIG_MARKOV_WEIGHT_RELATION).label(
                                               'rating')). \
                    join(word_b, word_b.id == f_id). \
                    outerjoin(WordRelation, and_(WordRelation.a_id == word_a.id, WordRelation.b_id == word_b.id)). \
                    outerjoin(WordNeighbor,
                              and_(word_a.id == WordNeighbor.b_id, WordNeighbor.a_id == subject_word.id)). \
                    filter(or_(WordNeighbor.rating > 0, WordNeighbor.rating == None)). \
                    filter(or_(WordRelation.rating > 0, WordRelation.rating == None)). \
                    filter(not_(and_(WordNeighbor.rating == None, WordRelation.rating == None)))

                if not topic_me:
                    query = query.filter(word_a.text != CONFIG_DISCORD_ME_SHORT)

                query = query.group_by(word_a.id). \
                    order_by(desc('rating')). \
                    limit(CONFIG_MARKOV_GENERATE_LIMIT)

                results = query.all()

            # Fall back to random
            if CONFIG_MARKOV_FALLBACK_RANDOM and len(results) == 0:
                results = self.session.query(WordRelation.a_id.label('id'), Word.text, Word.pos_id). \
                    join(Word, WordRelation.b_id == Word.id). \
                    order_by(desc(WordRelation.rating)). \
                    filter(and_(WordRelation.b_id == f_id, WordRelation.a_id != WordRelation.b_id)).all()

            if len(results) == 0:
                break

            r_index = int(np.random.triangular(0.0, 0.0, 1.0) * len(results))

            r = results[r_index]
            last_word = r

            f_id = r.id

            # Get Pos
            chosen_word_pos = self.session.query(Pos.text).filter(Pos.id == r.pos_id).first()
            mode = self.capitalization_model.predict(r.text, pos=chosen_word_pos.text,
                                                     word_index=backward_count - (count + 1))

            backwards_words.insert(0, CapitalizationMode.transform(mode, r.text,
                                                                   ignore_prefix_regexp=CONFIG_CAPITALIZATION_TRANSFORM_IGNORE_PREFIX))

            count += 1

        # Generate Forwards
        forward_words = []
        f_id = subject_word.id

        count = 0
        while count < forward_count:

            pos_index += 1
            choice = sentence_structure[pos_index_forward]

            # Most Intelligent search for next word (neighbor and pos)
            word_a = aliased(Word)
            word_b = aliased(Word)

            query = self.session.query(word_b.id, word_b.text, word_b.pos_id,
                                       (coalesce(sum(word_b.count), 0) * CONFIG_MARKOV_WEIGHT_WORDCOUNT
                                        + coalesce(sum(WordNeighbor.rating), 0) * CONFIG_MARKOV_WEIGHT_NEIGHBOR
                                        + coalesce(sum(WordRelation.rating),
                                                   0) * CONFIG_MARKOV_WEIGHT_RELATION).label(
                                           'rating')). \
                join(word_a, word_a.id == f_id). \
                join(Pos, Pos.id == word_b.pos_id). \
                outerjoin(WordNeighbor, and_(word_b.id == WordNeighbor.b_id, WordNeighbor.a_id == subject_word.id)). \
                outerjoin(WordRelation, and_(WordRelation.a_id == word_a.id, WordRelation.b_id == word_b.id)). \
                filter(Pos.text == choice). \
                filter(or_(WordNeighbor.rating > 0, WordNeighbor.rating == None)). \
                filter(or_(WordRelation.rating > 0, WordRelation.rating == None)). \
                filter(not_(and_(WordNeighbor.rating == None, WordRelation.rating == None)))

            if not topic_me:
                query = query.filter(word_b.text != CONFIG_DISCORD_ME_SHORT)

            query = query.group_by(word_b.id). \
                order_by(desc('rating')). \
                limit(CONFIG_MARKOV_GENERATE_LIMIT)

            results = query.all()

            if len(results) == 0:
                query = self.session.query(word_b.id, word_b.text, word_b.pos_id,
                                           (coalesce(sum(word_b.count), 0) * CONFIG_MARKOV_WEIGHT_WORDCOUNT
                                            + coalesce(sum(WordNeighbor.rating), 0) * CONFIG_MARKOV_WEIGHT_NEIGHBOR
                                            + coalesce(sum(WordRelation.rating),
                                                       0) * CONFIG_MARKOV_WEIGHT_RELATION).label(
                                               'rating')). \
                    join(word_a, word_a.id == f_id). \
                    outerjoin(WordRelation, and_(WordRelation.a_id == word_a.id, WordRelation.b_id == word_b.id)). \
                    outerjoin(WordNeighbor,
                              and_(word_b.id == WordNeighbor.b_id, WordNeighbor.a_id == subject_word.id)). \
                    filter(or_(WordNeighbor.rating > 0, WordNeighbor.rating == None)). \
                    filter(or_(WordRelation.rating > 0, WordRelation.rating == None)). \
                    filter(not_(and_(WordNeighbor.rating == None, WordRelation.rating == None)))

                if not topic_me:
                    query = query.filter(word_b.text != CONFIG_DISCORD_ME_SHORT)

                query = query.group_by(word_b.id). \
                    order_by(desc('rating')). \
                    limit(CONFIG_MARKOV_GENERATE_LIMIT)

                results = query.all()

            # Fall back to random
            if CONFIG_MARKOV_FALLBACK_RANDOM and len(results) == 0:
                results = self.session.query(WordRelation.b_id.label('id'), Word.text, Word.pos_id). \
                    join(Word, WordRelation.b_id == Word.id). \
                    order_by(desc(WordRelation.rating)). \
                    filter(and_(WordRelation.a_id == f_id, WordRelation.b_id != WordRelation.a_id)).all()

            if len(results) == 0:
                break

            r_index = int(np.random.triangular(0.0, 0.0, 1.0) * len(results))

            r = results[r_index]

            last_word = r

            f_id = r.id

            # Get Pos
            chosen_word_pos = self.session.query(Pos.text).filter(Pos.id == r.pos_id).first()
            mode = self.capitalization_model.predict(r.text, pos=chosen_word_pos.text)

            forward_words.append(
                CapitalizationMode.transform(mode, r.text, ignore_prefix_regexp=CONFIG_CAPITALIZATION_TRANSFORM_IGNORE_PREFIX))

            count += 1

        # Capitalization of subject
        if len(backwards_words) == 0:
            mode = self.capitalization_model.predict(subject_word.text, pos=subject_word.pos_text, word_index=0)
        else:
            mode = self.capitalization_model.predict(subject_word.text, pos=subject_word.pos_text)

        reply = []
        reply += backwards_words
        reply += [CapitalizationMode.transform(mode, subject_word.text,
                                               ignore_prefix_regexp=CONFIG_CAPITALIZATION_TRANSFORM_IGNORE_PREFIX)]
        reply += forward_words

        # Add a random URL
        if not no_url and random.randrange(0, 100) > (100 - CONFIG_MARKOV_URL_CHANCE):
            url = self.session.query(URL).order_by(func.random()).first()
            if url is not None:
                reply.append(url.text)

        return " ".join(reply)

    def check_reaction(self, input_message: MessageInput) -> None:
        bot_reply = self.reply_tracker.get_reply(input_message)

        # Check if reply exists
        if bot_reply.timestamp is None:
            return

        # Only handle reactions from the last CONFIG_MARKOV_REACTION_TIMEDELTA_S seconds or if the message is fresh
        elif bot_reply.fresh is not True and input_message.args.timestamp > bot_reply.timestamp + \
                timedelta(seconds=CONFIG_MARKOV_REACTION_TIMEDELTA_S):
            return

        if self.reaction_model.predict(input_message.message_filtered):
            self.handle_reaction(input_message)
            return

        # If this wasn't a reaction, end the chain
        self.reply_tracker.human_reply(input_message)

    def handle_reaction(self, input_message: MessageInput) -> None:
        server_last_replies = self.reply_tracker.get_reply(input_message)

        # Uprate words and relations
        for token_index, token in enumerate(server_last_replies.tokens):

            word_a = token['word']

            if word_a.pos.text in CONFIG_MARKOV_REACTION_SCORE_POS:
                word_a.rating += CONFIG_MARKOV_REACTION_UPRATE_WORD

                if token_index >= len(server_last_replies.tokens) - 1:
                    continue

                if 'word_a->b' in token:
                    word_b = token['word_a->b'].b
                    if word_b.pos.text in CONFIG_MARKOV_REACTION_SCORE_POS:
                        word_b.rating += CONFIG_MARKOV_REACTION_UPRATE_WORD
                        a_b_assoc = token['word_a->b']
                        a_b_assoc.rating += CONFIG_MARKOV_REACTION_UPRATE_RELATION

        # Uprate neighborhood
        for token in server_last_replies.tokens:

            # Filter things that are not relevant to the main information in a sentence
            if token['nlp'].pos_ not in CONFIG_MARKOV_NEIGHBORHOOD_POS_ACCEPT:
                continue

            for neighbor in token['word_neighbors']:

                # Filter things that are not relevant to the main information in a sentence
                if neighbor.b.pos.text not in CONFIG_MARKOV_NEIGHBORHOOD_POS_ACCEPT:
                    continue

                neighbor.count += 1
                neighbor.rating += CONFIG_MARKOV_REACTION_UPRATE_NEIGHBOR

        self.session.commit()

    def learn_url(self, input_message: MessageInput) -> None:
        for url in input_message.urls:

            the_url = self.session.query(URL).filter(URL.text == url).first()

            if the_url is not None:
                the_url.count += 1
            else:
                self.session.add(URL(text=url, timestamp=input_message.args.timestamp))

            self.session.commit()

    def process_msg(self, io_module, input_message: MessageInput, replyrate: int = 0,
                    rebuild_db: bool = False) -> None:

        # Ignore external I/O while rebuilding
        if self.rebuilding is True and not rebuild_db:
            return

        # Command message?
        if type(input_message) == MessageInputCommand:
            # noinspection PyTypeChecker
            reply = self.command(input_message)
            if reply:
                output_message = MessageOutput(text=reply)
                output_message.process(self.nlp)
                output_message.args.channel = input_message.args.channel
                output_message.args.timestamp = input_message.args.timestamp

                io_module.output(output_message)
            return

        # Keep pos_tree_model up to date with names of people for PoS detection
        if input_message.people is not None:
            self.pos_tree_model.update_people(input_message.people)

        # Log this line only if we are not rebuilding the database
        if not rebuild_db:

            # Sometimes server_id and channel can be none
            server_id = None
            if input_message.args.server is not None:
                # noinspection PyUnusedLocal
                server_id = server_id = input_message.args.server_id

            channel = None
            if input_message.args.channel is not None:
                channel = str(input_message.args.channel)

            self.session.add(
                Line(text=input_message.message_raw, author=input_message.args.author,
                     server_id=server_id, channel=channel,
                     timestamp=input_message.args.timestamp))
            self.session.commit()

        # Populate ORM and NLP POS data
        input_message.load(self.session, self.nlp)

        # Only want to check reaction when message on a server
        if input_message.args.server is not None and not input_message.args.author == CONFIG_DISCORD_ME:
            self.check_reaction(input_message)

        # Don't learn from ourself
        if input_message.args.learning and not input_message.args.author == CONFIG_DISCORD_ME:

            if CONFIG_DISCORD_MINI_ME is None or (
                            CONFIG_DISCORD_MINI_ME is not None and input_message.args.author in CONFIG_DISCORD_MINI_ME):
                self.learn_url(input_message)
                self.learn(input_message)

                if not rebuild_db:
                    self.pos_tree_model.process_text(input_message.message_filtered, update_prob=True)

        # Don't reply when rebuilding the database
        if not rebuild_db and (
                        replyrate > random.randrange(0, 100) or input_message.args.always_reply):

            reply = self.reply(input_message)

            if reply is None:
                return

            # Add response to lines
            # Offset timestamp by one second for database ordering
            reply_time_db = input_message.args.timestamp + timedelta(seconds=1)

            line = Line(text=reply, author=CONFIG_DISCORD_ME, server_id=input_message.args.server_id,
                        channel=input_message.args.channel_str, timestamp=reply_time_db)
            self.session.add(line)
            self.session.commit()

            output_message = MessageOutput(line=line)

            # We want the discord channel object to respond to and the original timestamp
            output_message.args.channel = input_message.args.channel
            output_message.args.channel_str = input_message.args.channel_str
            output_message.args.timestamp = input_message.args.timestamp

            # Load the reply database objects for reaction tracking
            output_message.load(self.session, self.nlp)

            self.reply_tracker.bot_reply(output_message)

            io_module.output(output_message)

        # If the author is us while we are rebuilding the DB, update the reply tracker
        elif rebuild_db and input_message.args.author == CONFIG_DISCORD_ME:
            # noinspection PyTypeChecker
            self.reply_tracker.bot_reply(input_message)

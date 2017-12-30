import signal
import sys
from enum import Enum, unique
from multiprocessing import Event

from common.nlp import create_nlp_instance
from config.ml import *
from markov_engine import MarkovTrieDb
from models.structure import StructureModelScheduler


@unique
class AEStatus(Enum):
    STARTING_UP = 1
    RUNNING = 2
    SHUTTING_DOWN = 3
    SHUTDOWN = 4


class ArmchairExpert(object):
    def __init__(self):
        # Placeholders
        self._markov_model = None
        self._nlp = None
        self._status = None
        self._structure_scheduler = None
        self._connectors = []
        self._connectors_event = Event()
        self._twitter_connector = None
        self._discord_connector = None

    def _set_status(self, status: AEStatus):
        self._status = status
        print("armchair-expert status: %s" % str(self._status).split(".")[1])

    def start(self):
        self._set_status(AEStatus.STARTING_UP)

        # Initialize backends and models
        self._markov_model = MarkovTrieDb()
        self._markov_model.load(MARKOV_DB_PATH)

        self._structure_scheduler = StructureModelScheduler(USE_GPU)
        self._structure_scheduler.start()
        self._structure_scheduler.load(STRUCTURE_MODEL_PATH)

        # Initialize connectors
        try:
            from config.twitter import TWITTER_CREDENTIALS
            from connectors.twitter import TwitterFrontend, TwitterReplyGenerator
            twitter_reply_generator = TwitterReplyGenerator(markov_model=self._markov_model,
                                                            structure_scheduler=self._structure_scheduler)
            self._twitter_connector = TwitterFrontend(reply_generator=twitter_reply_generator,
                                                      connectors_event=self._connectors_event,
                                                      credentials=TWITTER_CREDENTIALS)
            self._twitter_connector.start()
            self._connectors.append(self._twitter_connector)
        except ImportError:
            pass

        try:
            from config.discord import DISCORD_CREDENTIALS
            from connectors.discord import DiscordFrontend, DiscordReplyGenerator
            discord_reply_generator = DiscordReplyGenerator(markov_model=self._markov_model,
                                                            structure_scheduler=self._structure_scheduler)
            self._discord_connector = DiscordFrontend(reply_generator=discord_reply_generator,
                                                      connectors_event=self._connectors_event,
                                                      credentials=DISCORD_CREDENTIALS)
            self._discord_connector.start()
            self._connectors.append(self._discord_connector)
        except ImportError:
            pass

        # Non forking initializations
        self._nlp = create_nlp_instance()

        for frontend in self._connectors:
            frontend.give_nlp(self._nlp)

        # Handle events
        self._main()

    def _main(self):
        self._set_status(AEStatus.RUNNING)
        while True:
            if self._connectors_event.wait(timeout=0.2):
                self._connectors_event.clear()

                for frontend in self._connectors:
                    message = frontend.recv()
                    if message is not None:
                        reply = frontend.generate(message)
                        frontend.send(reply)
                    else:
                        frontend.send(None)

            if self._status == AEStatus.SHUTTING_DOWN:
                self.shutdown()
                self._set_status(AEStatus.SHUTDOWN)
                sys.exit(0)

    def shutdown(self):

        # Shutdown frontends
        for connector in self._connectors:
            connector.shutdown()

        # Save Models
        # self._markov_model.save(MARKOV_DB_PATH)
        # self._postree_model.save(POSTREE_DB_PATH)
        # self._capitalization_model.save(CAPITALIZATION_MODEL_PATH)

        # Shutdown Models
        self._structure_scheduler.shutdown()

    def handle_shutdown(self):
        # Shutdown main()
        self._set_status(AEStatus.SHUTTING_DOWN)


def signal_handler(sig, frame):
    if sig == signal.SIGINT:
        ae.handle_shutdown()


if __name__ == '__main__':
    signal.signal(signal.SIGINT, signal_handler)

    ae = ArmchairExpert()
    ae.start()

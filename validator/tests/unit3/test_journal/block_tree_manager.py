# Copyright 2016 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ------------------------------------------------------------------------------

from concurrent.futures import Executor
import hashlib
import pprint
import random
import string

from sawtooth_signing import pbct as signing

from sawtooth_validator.journal.journal import \
    BlockPublisher
from sawtooth_validator.journal.consensus.test_mode.test_mode_consensus \
    import \
    BlockPublisher as TestModePublisher
from sawtooth_validator.journal.consensus.test_mode.test_mode_consensus \
    import \
    BlockVerifier as TestModeVerifier

from sawtooth_validator.protobuf.block_pb2 import Block
from sawtooth_validator.protobuf.block_pb2 import BlockHeader
from sawtooth_validator.protobuf.batch_pb2 import Batch
from sawtooth_validator.protobuf.batch_pb2 import BatchHeader
from sawtooth_validator.protobuf.transaction_pb2 import Transaction
from sawtooth_validator.protobuf.transaction_pb2 import TransactionHeader

from sawtooth_validator.journal.block_builder import BlockBuilder
from sawtooth_validator.journal.block_cache import BlockCache
from sawtooth_validator.journal.block_store_adapter import BlockStoreAdapter
from sawtooth_validator.journal.block_wrapper import NULL_BLOCK_IDENTIFIER
from sawtooth_validator.journal.block_wrapper import BlockStatus
from sawtooth_validator.journal.block_wrapper import BlockWrapper

from test_journal.mock import MockBlockSender
from test_journal.mock import MockTransactionExecutor

pp = pprint.PrettyPrinter(indent=4)


def _generate_id(length=16):
    return ''.join(
        random.SystemRandom().choice(string.ascii_uppercase + string.digits)
        for _ in range(length))


class BlockTreeManager(object):
    def block_def(self,
                  add_to_store=False,
                  add_to_cache=False,
                  batch_count=0,
                  status=BlockStatus.Unknown,
                  invalid_consensus=False,
                  invalid_batch=False,
                  invalid_signature=False,
                  weight=None
                  ):
        return {
            "add_to_cache": add_to_cache,
            "add_to_store": add_to_store,
            "batch_count": batch_count,
            "status": status,
            "invalid_consensus": invalid_consensus,
            "invalid_batch": invalid_batch,
            "invalid_signature": invalid_signature,
            "weight": weight
        }

    def __init__(self):
        self.block_sender = MockBlockSender()
        self.block_store = BlockStoreAdapter({})
        self.block_cache = BlockCache(self.block_store)

        self.signing_key = signing.generate_privkey()
        self.public_key = signing.encode_pubkey(
            signing.generate_pubkey(self.signing_key), "hex")
        self.genesis_block = self._generate_genesis_block()
        self.block_store[self.genesis_block.identifier] = self.genesis_block
        self.set_chain_head(self.genesis_block)

        self.block_publisher = BlockPublisher(
            consensus=TestModePublisher(),
            transaction_executor=MockTransactionExecutor(),
            block_sender=self.block_sender,
            squash_handler=None,
            chain_head=self.genesis_block)

    def _get_block_id(self, block):
        if (block is None):
            return None
        elif isinstance(block, Block) or\
                isinstance(block, BlockWrapper):
            return block.header_signature
        elif isinstance(block, basestring):
            return block
        else:
            return str(block)

    def _get_block(self, block):
        if (block is None):
            return None
        elif isinstance(block, Block):
            return BlockWrapper(block)
        elif isinstance(block, BlockWrapper):
            return block
        elif isinstance(block, str):
            return self.block_cache[block]
        else:  # WTF try something crazy
            return self.block_cache[str(block)]

    def set_chain_head(self, block):
        self.block_store.set_chain_head(self._get_block_id(block))

    @property
    def chain_head(self):
        return self.block_store.chain_head

    def set_chain_head(self, block):
        self.block_store.set_chain_head(block.identifier)

    def generate_block(self, previous_block=None,
                       add_to_store=False,
                       add_to_cache=False,
                       batch_count=0,
                       status=BlockStatus.Unknown,
                       invalid_consensus=False,
                       invalid_batch=False,
                       invalid_signature=False,
                       weight=None):

        previous = self._get_block(previous_block)
        if previous is None:
            previous = self.chain_head

        self.block_publisher.on_chain_updated(previous)

        while self.block_sender.new_block is None:
            self.block_publisher.on_batch_received(Batch())
            self.block_publisher.on_check_publish_block(True)

        block = self.block_sender.new_block
        self.block_sender.new_block = None

        block = BlockWrapper(block)

        if invalid_signature:
            block.block.header_signature = "BAD"

        block.weight = weight
        block.status = status

        if add_to_cache:
            self.block_cache[block.identifier] = block

        if add_to_store:
            if block.weight is None:
                tmv = TestModeVerifier()
                block.weight = tmv.compute_block_weight(block)
            self.block_store[block.identifier] = block

        return block

    def _generate_batch(self, payload):
        payload_encoded = payload.encode('utf-8')
        hasher = hashlib.sha512()
        hasher.update(payload_encoded)

        header = TransactionHeader()
        header.batcher_pubkey = self.public_key
        # txn.dependencies not yet
        header.family_name = 'test'
        header.family_version = '1'
        header.nonce = _generate_id(16)
        header.payload_encoding = "text"
        header.payload_sha512 = hasher.hexdigest().encode()
        header.signer_pubkey = self.public_key

        txn = Transaction()
        header_bytes = header.SerializeToString()
        txn.header = header_bytes
        txn.header_signature = signing.sign(header_bytes, self.signing_key)
        txn.payload = payload_encoded

        batch_header = BatchHeader()
        batch_header.signer_pubkey = self.public_key
        batch_header.transaction_ids.extend([txn.header_signature])

        batch = Batch()
        header_bytes = batch_header.SerializeToString()
        batch.header = header_bytes
        batch.header_signature = signing.sign(header_bytes, self.signing_key)
        batch.transactions.extend([txn])
        return batch

    def _generate_genesis_block(self):
        genesis_header = BlockHeader(
            previous_block_id=NULL_BLOCK_IDENTIFIER,
            signer_pubkey=self.public_key,
            block_num=0)

        block = BlockBuilder(genesis_header)
        block.add_batches([self._generate_batch("Genesis")])
        header_bytes = block.block_header.SerializeToString()
        signature = signing.sign(header_bytes, self.signing_key)
        block.set_signature(signature)
        return BlockWrapper(block.build_block())

    def generate_chain(self, root_block, blocks, params=None):
        """
        Generate a new chain based on the root block and place it in the
        block cache.
        """
        if params is None:
            params = {}

        out = []
        if not isinstance(blocks, list):
            blocks = self.generate_chain_definition(int(blocks), params)

        if root_block is None:
            previous = self._generate_genesis_block()
            self.block_store[previous.identifier] = previous
        else:
            previous = self._get_block(root_block)
        for block in blocks:
            new_block = self.generate_block(previous_block=previous, **block)
            out.append(new_block)
            previous = new_block
        return out

    def generate_chain_definition(self, count, params=None):
        if params is None:
            params = {}

        out = []
        for _ in range(0, count):
            out.append(self.block_def(**params))
        return out

    def __str__(self):
        return str(self.block_cache)

    def __repr__(self):
        return repr(self.block_cache)

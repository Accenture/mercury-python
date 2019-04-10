import unittest
import asyncio
from mercury.system.diskqueue import ElasticQueue


class TestDiskQueue(unittest.TestCase):

    def test_read_write(self):
        byte_value = b'0x01'
        total = 10
        queue = ElasticQueue(queue_dir='/tmp', queue_id='test')

        async def test_write():
            for n in range(total):
                await queue.write({'v': 'hello world', 'n': n, 'b': byte_value})

        loop = asyncio.get_event_loop()
        loop.run_until_complete(test_write())

        for i in range(total):
            s = queue.read()
            self.assertIsNotNone(s)
            self.assertTrue('n' in s)
            self.assertTrue('v' in s)
            self.assertTrue('b' in s)
            self.assertEqual(s['n'], i)
            self.assertEqual(s['b'], byte_value)

        queue.close()
        queue.destroy()
        self.assertTrue(queue.is_closed())

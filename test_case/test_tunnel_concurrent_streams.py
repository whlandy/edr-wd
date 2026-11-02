import socket
import threading

from agent.tunnel import _start_forward


def test_persistent_stream_does_not_block_second_request():
    channels = []
    peers = []
    workers = []

    class Transport:
        def open_channel(self, *args, **kwargs):
            left, right = socket.socketpair()
            channels.append(left)
            peers.append(right)
            return left

    clients = []
    transport = Transport()
    try:
        for i in range(2):
            client, accepted = socket.socketpair()
            client.settimeout(2)
            clients.append(client)
            workers.append(_start_forward(accepted, transport, 8765))
            # Two _start_forward threads race to open_channel, so the two
            # socketpairs would land in channels/peers in either order.
            # Wait for this connection's channel before opening the next so
            # peers[i] is guaranteed to correspond to clients[i].
            deadline = threading.Event()
            for _ in range(200):
                if len(peers) == i + 1:
                    break
                deadline.wait(.01)
            assert len(peers) == i + 1
        # Both connections must be established while the first is still open.
        assert len(peers) == 2
        clients[1].sendall(b'POST second')
        peers[1].settimeout(2)
        assert peers[1].recv(64) == b'POST second'
        peers[1].sendall(b'result')
        assert clients[1].recv(64) == b'result'
        assert workers[0].is_alive()
    finally:
        for sock in clients + peers:
            sock.close()
        for worker in workers:
            worker.join(timeout=2)

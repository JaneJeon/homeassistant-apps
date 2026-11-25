# iperf3 server

Runs the Alpine-packaged `iperf3 -s` in the foreground. There are no options.

Start the app, then from a client on the LAN:

```sh
iperf3 -c <HA Green address> -t 15        # client to Green
iperf3 -c <HA Green address> -t 15 -R     # Green to client
iperf3 -c <HA Green address> -t 15 -P 4   # four parallel streams
```

Port 5201 (TCP and UDP) is published on the host. The app does not start at boot;
stop it when finished. It has no authentication, so keep the port off the public
internet.

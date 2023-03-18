import telnetlib, sys, threading, re
from collections import defaultdict

HOST, PORT = 't2tmud.org', 9999

class T2TMUDClient:
    def __init__(self, h, p):
        self.host, self.port, self.connection, self.triggers = h, p, None, defaultdict(str)
        self.log = []

    def connect(self, output):
        self.output = output
        self.connection = telnetlib.Telnet(self.host, self.port)
        threading.Thread(target=self.listen, daemon=True).start()

    def listen(self):
        try:
            while (data := self.connection.read_until(b'\n').decode('ascii')):
                self.log.append(('server', data.strip()))
                self.output(data)
                self.check_triggers(data)
        except EOFError:
            self.log.append(('error', 'Connection closed.'))
            self.output("Connection closed.", 1)
            sys.exit(0)
        
    def send(self, cmd):
        self.connection.write(f"{cmd}\n".encode('ascii'))
        self.log.append(('client', cmd.strip()))

    def close(self):
        self.connection.close()

    def add_trigger(self, p, cmd):
        self.triggers[p] = cmd

    def check_triggers(self, data):
        for p, cmd in self.triggers.items():
            if re.search(p, data):
                self.send(cmd)

def print_out(text):
    print(text, end='')

def main():
    client = T2TMUDClient(HOST, PORT)
    client.connect(print_out)

    cmd = input("")
    client.send(cmd)
    try:
        while True:
          cmd = input("")
          if cmd.strip() == "quit":
            client.close()  # Close the old connection
            client = T2TMUDClient(HOST, PORT)  # Create a new client instance
            client.connect(print_out)  # Connect to the server with the new client
          else:
            client.send(cmd)
              
    except (EOFError, KeyboardInterrupt):
        client.close()
        print("Disconnected.")
        client = T2TMUDClient(HOST, PORT)  # Create a new client instance
        client.connect(print_out)
      
if __name__ == "__main__":
    main()

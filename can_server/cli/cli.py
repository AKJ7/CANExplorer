import click


@click.group()
def cli():
    pass


@cli.command()
@click.option('--server-ip', default='localhost', help='IP of the CAN server')
def setup_server(server_ip: str):
    click.echo(f'Received server IP: {server_ip}')

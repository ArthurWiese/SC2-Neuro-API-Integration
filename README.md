# SC2 Neuro API Integration
An integration of the Neuro API for StarCraft 2

In the [Mod](Mod) and [Maps](Maps) folders is a demo for the implementation of the Neuro API in the first Mission of the Wings of Liberty campaign

Tested with [Gary](https://github.com/Govorunb/gary)

## What I want to use this integration for
My goal is to create a custom Wings of Liberty campaign experience where Neuro gets context, triggers effects in the game and selects, gains and uses permanent abilities over the course of the campaign

For proposing ideas and current progress see [SC2 Neuro WoL Integration plan]()

If you want to contribute to the SC2 Neuro WoL Integration see [Neuro-sama Discord]() under projects/SC2 Neuro WoL Integration

## Getting started
Note: Should only work on Windows for now (Because of file paths, etc...)

1. Download or clone the repo
2. To play a demo of the integration in action copy the [.SC2Mod folder](Mod) into the Mods folder in your StarCraft 2 installation:
```
...\StarCraft II\Mods\<Here .SC2Mod folder>
```
Copy the [.SC2Map folders](Maps) into the Maps\Campaign folder (maybe need to create the Campaign folder) in your StarCraft 2 installation:
```
...\StarCraft II\Maps\Campaign\<Here .SC2Map folders>
```
3. Install the required modules for Python ([requirements.txt](requirements)) or create an environment with [environment.yml](environment)
4. Start the integration terminal by running [SC2_integration.py](SC2_integration) with Python
5. Set the banks path in the integration terminal
```
banks_path <...\Documents\StarCraft II\Accounts\...\...\Banks>
```
and set the websocket server URL
```
neuro_url <URL>
```
6. Start the Neuro integration in the integration terminal with
```
start
```
7. For the demo start a new Wings of Liberty campaign in StarCraft 2 and start the first mission.


Note: You can launch StarCraft 2 listening to the SC2API after setting the StarCraft 2 installation folder and giving the "launch" command.
You can then currently only send Ping messages to the game. 
This method of communicating with the game turned out to be a dead end for creating a custom campaign but maybe interesting for letting Neuro play the game herself

## Documentation
See documentation to learn how to mod StarCraft 2 to work with the Neuro API: 

[SC2 Neuro API Integration Documentation]()

## Licensing
- Original maps are owned by Blizzard®
- This repo contains original Blizzard® assets. These are part of the base game and licensed by their terms
- This repo contains code from from the StarCraft II Client - protocol by Blizzard®. See [License](License (s2client-proto)). See [Repo](https://github.com/Blizzard/s2client-proto)
- This repo contains code form the StarCraft II API Client for Python 3 by BurnySc2. See [License](License (python-sc2)). See [Repo](https://github.com/BurnySc2/python-sc2)
- Otherwise HERE LICENSE applies. See [License]()

Blizzard is a registered trademark of Blizzard Entertainment, Inc

Wings of Liberty and StarCraft are a trademarks or registered trademark of Blizzard Entertainment, Inc., in the U.S. and/or other countries

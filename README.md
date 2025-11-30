# CANExplorer
A CANbus-Analyse Tool for all platforms.

## Usage  
- Install dependencies
  ```bash
  poetry lock && poetry install
  ```
- Run main application
  ```bash
  poetry run main <ARGS>
  ```


## Roadmap
1. Project Setup
    - [x] Initialize git project  
    - [x] Set basic project structure 
    - [x] Set package manager and install packages
    - [x] Set docker and vscode devcontainer
    - [x] Add parsing and transmission of CAN frame as proof of concept
    - [x] Release 0.1.0
2. Transception of CAN frames.
    - [ ] Implement IsoCan transport and protocol
    - [ ] Implement J1939 transport and protocol
    - [ ] Implement ISOTP transport and protocol
    - [ ] Implement CanOpen transport and protocol
    - [ ] Release 0.2.0
3. Move UI to QML
    - [ ] Add main UI
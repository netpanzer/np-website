#!/bin/bash

sudo cp np-website.service /etc/systemd/system/
sudo systemctl enable np-website
sudo systemctl restart np-website

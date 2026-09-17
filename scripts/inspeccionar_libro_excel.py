#!/usr/bin/env python3
"""Imprime las primeras filas de pestañas de un libro XLSX/XLSM sin modificarlo."""
from __future__ import annotations

import re
import sys
import zipfile
from xml.etree import ElementTree as ET

MAIN = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PACKAGE_REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
NS = {'m': MAIN, 'r': REL, 'p': PACKAGE_REL}


def column_index(reference):
    number = 0
    for letter in re.match(r'[A-Z]+', reference).group(0):
        number = number * 26 + ord(letter) - 64
    return number - 1


def sheets(path):
    with zipfile.ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read('xl/workbook.xml'))
        rels = ET.fromstring(archive.read('xl/_rels/workbook.xml.rels'))
        targets = {item.attrib['Id']: item.attrib['Target'] for item in rels.findall('p:Relationship', NS)}
        return {item.attrib['name']: 'xl/' + targets[item.attrib[f'{{{REL}}}id']].lstrip('/')
                for item in workbook.find('m:sheets', NS)}


def rows(path, name):
    with zipfile.ZipFile(path) as archive:
        shared_root = ET.fromstring(archive.read('xl/sharedStrings.xml'))
        shared = [''.join(item.itertext()) for item in shared_root.findall('m:si', NS)]
        sheet = ET.fromstring(archive.read(sheets(path)[name]))
    values = []
    for row in sheet.findall('.//m:sheetData/m:row', NS):
        record = []
        for cell in row.findall('m:c', NS):
            index = column_index(cell.attrib['r'])
            while len(record) <= index:
                record.append('')
            value = cell.find('m:v', NS)
            raw = value.text if value is not None else ''
            record[index] = shared[int(raw)] if cell.attrib.get('t') == 's' and raw else raw
        values.append(record)
    return values


if __name__ == '__main__':
    path = sys.argv[1]
    names = sys.argv[2:] or list(sheets(path))
    for name in names:
        data = rows(path, name)
        print(f'--- {name} ({len(data)} filas) ---')
        for row in data[:12]:
            print(row)

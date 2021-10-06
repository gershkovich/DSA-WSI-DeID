import datetime
import json
import os
import shutil
import subprocess
import tempfile
import paramiko

import jsonschema
import magic
import openpyxl
import pandas as pd
from enum import Enum
from girder import logger, events
from girder.models.assetstore import Assetstore
from girder.models.file import File
from girder.models.folder import Folder
from girder.models.item import Item
from girder.models.setting import Setting
from girder.models.upload import Upload
from girder_large_image.models.image_item import ImageItem

from . import process
from . import config
from .constants import PluginSettings

XLSX_MIMETYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

class SftpMode(Enum):
    LOCAL_EXPORT_ONLY = 0
    SFTP_ONLY = 1
    SFTP_AND_EXPORT = 2


def readExcelData(filepath):
    """
    Read in the data from excel, while attempting to be forgiving about
    the exact location of the header row.

    :param filepath: path to the excel file.
    :returns: a pandas dataframe of the excel data rows.
    :returns: the header row number.
    """
    potential_header = 0
    reader = pd.read_csv
    mimetype = magic.from_file(filepath, mime=True)
    if 'excel' in mimetype or 'openxmlformats' in mimetype:
        reader = pd.read_excel
    df = reader(filepath, header=potential_header, dtype=str)
    rows = df.shape[0]
    while potential_header < rows:
        # When the columns include TokenID, ImageID, this is the Header row.
        if (
                'TokenID' in df.columns and
                'ImageID' in df.columns and
                any(key in df.columns for key in {'ScannedFileName', 'InputFileName'})):
            return df, potential_header
        potential_header += 1
        df = reader(filepath, header=potential_header, dtype=str)
    raise ValueError(f'Excel file {filepath} lacks a header row')


def validateDataRow(validator, row, rowNumber, df):
    """
    Validate a row from a dataframe with a jsonschema validator.

    :param validator: a jsonschema validator.
    :param row: a dictionary of row information from the dataframe excluding
        the Index.
    :param rowNumber: the 1-based row number within the file for error
        reporting.
    :param df: the pandas dataframe.  Used to determine column number.
    :returns: None for no errors, otherwise a list of error messages.
    """
    if validator.is_valid(row):
        return
    errors = []
    for error in validator.iter_errors(row):
        try:
            columnName = error.path[0]
            columnNumber = df.columns.get_loc(columnName)
            cellName = openpyxl.utils.cell.get_column_letter(columnNumber + 1) + str(rowNumber)
            errorMsg = f'Invalid {columnName} in {cellName}'
        except Exception:
            errorMsg = f'Invalid row {rowNumber} ({error.message})'
            columnNumber = None
        errors.append(errorMsg)
    if row['ImageID'] != '%s_%s_%s' % (row['TokenID'], row['Proc_Seq'], row['Slide_ID']):
        errors.append(
            f'Invalid ImageID in row {rowNumber}; not composed of TokenID, Proc_Seq, and Slide_ID')
    return errors


def getSchemaValidator():
    """
    Return a jsonschema validator.

    :returns: a validator.
    """
    return jsonschema.Draft6Validator(json.load(open(os.path.join(
        os.path.dirname(__file__), 'schema', 'importManifestSchema.json'))))


def readExcelFiles(filelist, ctx):
    """
    Read each excel file, use pandas to parse it.  Collect the results, where,
    if a file is stored twice, the value from the newest excel file wins.

    :param filelist: a list of excel or csv file paths.
    :param ctx: a process context.
    :returns: a dictionary with scanned file names as the keys.  Each entry
        contains an ImageID, TokenID, name (the scanned file name), excel (the
        path from the excel file), and timestamp (the mtime of the excel file).
    """
    manifest = {}
    report = []
    validator = getSchemaValidator()
    for filepath in filelist:
        ctx.update(message='Reading %s' % os.path.basename(filepath))
        try:
            df, header_row_number = readExcelData(filepath)
            df = df.dropna(how='all', axis='columns')
        except Exception as exc:
            if isinstance(exc, ValueError):
                message = 'Cannot read %s; it is not formatted correctly' % (
                    os.path.basename(filepath), )
                status = 'badformat'
            else:
                message = 'Cannot read %s; it is not an Excel file' % (
                    os.path.basename(filepath), )
                status = 'notexcel'
            report.append({
                'path': filepath,
                'status': status,
                'reason': message,
            })
            ctx.update(message=message)
            logger.info(message)
            continue
        timestamp = os.path.getmtime(filepath)
        count = 0
        totalErrors = []
        for row_num, row in enumerate(df.itertuples()):
            rowAsDict = dict(row._asdict())
            rowAsDict.pop('Index')
            if all(not val or str(val) == 'nan' for val in rowAsDict.values()):
                continue
            errors = validateDataRow(validator, rowAsDict, header_row_number + 2 + row_num, df)
            name = None
            for key in {'ScannedFileName', 'InputFileName'}:
                name = rowAsDict.pop(key, name)
            if errors:
                for error in errors:
                    message = 'Error in %s: %s' % (os.path.basename(filepath), error)
                    ctx.update(message=message)
                    logger.info(message)
                totalErrors.append({'name': name, 'errors': errors})
            if not name:
                continue
            count += 1
            if name not in manifest or (timestamp > manifest[name]['timestamp'] and not errors):
                manifest[name] = {
                    'timestamp': timestamp,
                    'ImageID': row.ImageID,
                    'TokenID': row.TokenID,
                    'name': name,
                    'excel': filepath,
                    'fields': rowAsDict,
                    'errors': errors,
                }
        report.append({
            'path': filepath,
            'status': 'parsed',
            'count': count,
            'errors': totalErrors,
        })
        logger.info('Read %s; parsed %d valid rows' % (filepath, count))
    return manifest, report


def ingestOneItem(importFolder, imagePath, record, ctx, user):
    """
    Ingest a single image.

    :param importFolder: the folder to store the image.
    :param imagePath: the path of the image file.
    :param record: a dictionary of information from the excel file.
    :param ctx: a progress context.
    :param user: the user triggering this.
    """
    status = 'added'
    stat = os.stat(imagePath)
    existing = File().findOne({'path': imagePath, 'imported': True})
    if existing:
        if existing['size'] == stat.st_size:
            return 'present'
        item = Item().load(existing['itemId'], force=True)
        # TODO: move item somewhere; for now, delete it
        ctx.update(message='Removing existing %s since the size has changed' % imagePath)
        Item().remove(item)
        status = 'replaced'
    parentFolder = Folder().findOne({'name': record['TokenID'], 'parentId': importFolder['_id']})
    if not parentFolder:
        parentFolder = Folder().createFolder(importFolder, record['TokenID'], creator=user)
    # TODO: (a) use the getTargetAssetstore method from Upload(), (b) ensure
    # that the assetstore is a filesystem assestore.
    assetstore = Assetstore().getCurrent()
    name = record['ImageID'] + os.path.splitext(record['name'])[1]
    mimeType = 'image/tiff'
    if Item().findOne({'name': {'$regex': '^%s\\.' % record['ImageID']}}):
        return 'duplicate'
    item = Item().createItem(name=name, creator=user, folder=parentFolder)
    file = File().createFile(
        name=name, creator=user, item=item, reuseExisting=False,
        assetstore=assetstore, mimeType=mimeType, size=stat.st_size,
        saveFile=False)
    file['path'] = os.path.abspath(os.path.expanduser(imagePath))
    file['mtime'] = stat.st_mtime
    file['imported'] = True
    file = File().save(file)
    # Reload the item as it will have changed
    item = Item().load(item['_id'], force=True)
    item = Item().setMetadata(item, {'deidUpload': record['fields']})
    try:
        redactList = process.get_standard_redactions(item, record['ImageID'])
    except Exception:
        logger.exception('Failed to import %s' % name)
        Item().remove(item)
        ctx.update(message='Failed to import %s' % name)
        return 'failed'
    item = Item().setMetadata(item, {'redactList': redactList})
    ctx.update(message='Imported %s' % name)
    return status


def ingestData(ctx, user=None):  # noqa
    """
    Scan the import folder for image and excel files.  For each excel file,
    extract the appropriate data.  For each file listed in an excel file,
    if the import path is already in the system and has the same length as the
    file, do nothing.  If the length has changed, remove the existing item.
    For each listed file that is now not in the system, add it to the import
    directory with an appropriate name.  Add each listed file that is not in
    the system to a report; add each file present that is not listed to a
    report.  Emit the report.

    :param ctx: a progress context.
    :param user: the user triggering this.
    """
    importPath = Setting().get(PluginSettings.WSI_DEID_IMPORT_PATH)
    importFolderId = Setting().get(PluginSettings.HUI_INGEST_FOLDER)
    if not importPath or not importFolderId:
        raise Exception('Import path and/or folder not specified.')
    importFolder = Folder().load(importFolderId, force=True, exc=True)
    ctx.update(message='Scanning import folder')
    excelFiles = []
    imageFiles = []
    for base, _dirs, files in os.walk(importPath):
        for file in files:
            filePath = os.path.join(importPath, base, file)
            _, ext = os.path.splitext(file)
            if ext.lower() in {'.xls', '.xlsx', '.csv'} and not file.startswith('~$'):
                excelFiles.append(filePath)
            # ignore some extensions
            elif (ext.lower() not in {'.zip', '.txt', '.xml', '.swp', '.xlk'} and
                    not file.startswith('~$')):
                imageFiles.append(filePath)
    if not len(excelFiles):
        ctx.update(message='Failed to find any excel files in import directory.')
    if not len(imageFiles):
        ctx.update(message='Failed to find any image files in import directory.')
    manifest, excelReport = readExcelFiles(excelFiles, ctx)
    missingImages = []
    report = []
    for record in manifest.values():
        try:
            imagePath = os.path.join(os.path.dirname(record['excel']), record['name'])
        except TypeError:
            imagePath = None
        if imagePath not in imageFiles:
            imagePath = None
            for testPath in imageFiles:
                if os.path.basename(testPath) == record['name']:
                    imagePath = testPath
                    break
        if imagePath is None and not record.get('errors'):
            missingImages.append(record)
            status = 'missing'
            report.append({'record': record, 'status': status, 'path': record['name']})
            continue
        if imagePath is not None:
            imageFiles.remove(imagePath)
        if record.get('errors'):
            status = 'badentry'
        else:
            status = ingestOneItem(importFolder, imagePath, record, ctx, user)
        report.append({'record': record, 'status': status, 'path': imagePath})
    # imageFiles are images that have no manifest record
    for image in imageFiles:
        status = 'unlisted'
        report.append({'record': None, 'status': status, 'path': image})
    file = importReport(ctx, report, excelReport, user, importPath)
    return reportSummary(report, excelReport, file=file)


def importReport(ctx, report, excelReport, user, importPath):
    """
    Create an import report.

    :param ctx: a progress context.
    :param report: a list of files that were exported.
    :param excelReport: a list of excel files that were processed.
    :param user: the user triggering this.
    :param importPath: the path of the import folder.  Used to show relative
        paths in the report.
    :return: the Girder file with the report
    """
    ctx.update(message='Generating report')
    excelStatusDict = {
        'parsed': 'Parsed',
        'notexcel': 'Not Excel',
        'badformat': 'Bad Format',
    }
    statusDict = {
        'added': 'Imported',
        'present': 'Already imported',
        'replaced': 'Updated',
        'missing': 'File missing',
        'unlisted': 'Not in DeID Upload file',
        'badentry': 'Error in DeID Upload file',
        'failed': 'Failed to import',
        'duplicate': 'Duplicate ImageID',
    }
    statusExplanation = {
        'failed': 'Image file is not an accepted WSI format',
        'duplicate': 'A different image with the same ImageID was previously imported',
        'replaced': 'File size was different than that already present; '
                    'existing image was replaced',
    }
    keyToColumns = {
        'excel': 'ExcelFilePath',
    }
    dataList = []
    statusKey = 'SoftwareStatus'
    reasonKey = 'Status/FailureReason'
    anyErrors = False
    for row in excelReport:
        data = {
            'ExcelFilePath': os.path.relpath(row['path'], importPath),
            statusKey: excelStatusDict.get(row['status'], row['status']),
            reasonKey: row.get('reason'),
        }
        if row['status'] == 'badformat' and not row.get('reason'):
            data[reasonKey] = 'No header row with ImageID, TokenID, and ImportFileName'
        dataList.append(data)
        anyErrors = anyErrors or row['status'] in {'notexcel', 'badformat'}
    for row in report:
        data = {
            'WSIFilePath': os.path.relpath(row['path'], importPath) if row.get(
                'path') and row['status'] != 'missing' else row.get('path'),
            statusKey: statusDict.get(row['status'], row['status']),
        }
        if row.get('record'):
            fields = row['record'].get('fields')
            data.update(fields)
            for k, v in row['record'].items():
                if k == 'excel' and v:
                    v = os.path.relpath(v, importPath)
                if k != 'fields':
                    data[keyToColumns.get(k, k)] = v
            if row['record'].get('errors'):
                data[reasonKey] = '. '.join(row['record']['errors'])
        if not data.get(reasonKey) and row['status'] in statusExplanation:
            data[reasonKey] = statusExplanation[row['status']]
        dataList.append(data)
        anyErrors = anyErrors or row['status'] in {
            'duplicate', 'missing', 'unlisted', 'failed', 'badentry'}
    if not len(excelReport) and not len(report):
        dataList.insert(0, {
            reasonKey: 'Nothing to import.  Import folder is empty.'})
    else:
        dataList.insert(0, {
            reasonKey: 'Import process completed' if not anyErrors
                       else 'Import process completed with errors'})
    for row in dataList:
        if not row.get(reasonKey) and row.get(statusKey):
            row[reasonKey] = row[statusKey]
    df = pd.DataFrame(dataList, columns=[
        'ExcelFilePath', 'WSIFilePath', statusKey,
        'TokenID', 'Proc_Seq', 'Proc_Type', 'Spec_Site', 'Slide_ID', 'ImageID',
        reasonKey
    ])
    reportName = 'DeID Import Job %s.xlsx' % datetime.datetime.now().strftime('%Y%m%d %H%M%S')
    reportFolder = 'Import Job Reports'
    with tempfile.TemporaryDirectory(prefix='wsi_deid') as tempdir:
        path = os.path.join(tempdir, reportName)
        ctx.update(message='Saving report')
        df.to_excel(path, index=False)
        return saveToReports(path, XLSX_MIMETYPE, user, reportFolder)


def reportSummary(*args, **kwargs):
    """
    Generate a summary of multiple reports.

    :param *args: all other arguments are lists of results, each entry of which
        is a dictionary with a 'status' field.  The overall summary is a
        tally of the occurrences of each status.
    :param **kwargs: if 'file' is specified, this is a Girder file model.  The
        file id is returned as part of the results.
    :returns: a dictionary of status values, each with a numerical tally, and
        optionally a fileId field.
    """
    result = {}
    for report in args:
        for entry in report:
            result.setdefault(entry['status'], 0)
            result[entry['status']] += 1
    if kwargs.get('file'):
        result['fileId'] = str(kwargs['file']['_id'])
    return result


def exportItems(ctx, user=None, all=False):
    """
    Export all or all recent items in the Finished directory.  Mark each
    exported item as having been exported.

    :param ctx: a progress context.
    :param user: the user triggering this.
    :param all: True to export all items.  False to only export items that have
        not been previously exported.
    """
    sftp_mode = SftpMode(config.getConfig('sftp_mode', 0))
    export_enabled = sftp_mode in [SftpMode.LOCAL_EXPORT_ONLY, SftpMode.SFTP_AND_EXPORT]
    sftp_enabled = sftp_mode in [SftpMode.SFTP_AND_EXPORT, SftpMode.SFTP_ONLY]
    logger.info('Export begin (all=%s)' % all)
    exportPath = Setting().get(PluginSettings.WSI_DEID_EXPORT_PATH)
    exportFolderId = Setting().get(PluginSettings.HUI_FINISHED_FOLDER)
    if not exportPath or not exportFolderId:
        raise Exception('Export path and/or finished folder not specified.')
    exportFolder = Folder().load(exportFolderId, force=True, exc=True)
    report = []
    totalByteCount = 0
    if export_enabled:
        for mode in ('measure', 'copy'):
            byteCount = 0
            for filepath, file in Folder().fileList(exportFolder, user, data=False):
                byteCount += exportItemsNext(
                    mode, ctx, byteCount, totalByteCount, filepath, file, exportPath, user, report)
            totalByteCount = byteCount
        logger.info('Exported files')
        exportNoteRejected(report, user, all)
        logger.info('Exported note others')
        file = exportReport(ctx, exportPath, report, user)
        logger.info('Exported generated report')
        summary = reportSummary(report, file=file)
        logger.info('Exported done')
    if sftp_enabled:
        sftp_event_info = {'export_folder': exportFolder, 'user': user}
        events.daemon.trigger('wsi_deid.sftp_export', sftp_event_info)
    summary['sftp_enabled'] = sftp_enabled
    return summary


def sftp_items(export_folder, user):
    """
    Export items to a remote server via SFTP.

    :param sftp_mode: should come from the girder config file. Used to determine if SFTP is enabled
    :param export_folder: the girder folder from which files should be exported
    :param user: the user triggering the export
    """
    sftp_mode = config.getConfig('sftp_mode', 0)
    sftp_enabled = SftpMode(sftp_mode) in [SftpMode.SFTP_AND_EXPORT, SftpMode.SFTP_ONLY]
    sftp_destination = config.getConfig('sftp_destination_folder')
    if not sftp_enabled: # Sanity check
        return

    if not sftp_destination:
        raise Exception('SFTP destination not specified')

    logger.info('SFTP begin. Remote destination: %s', sftp_destination)
    sftp_client = get_sftp_client()
    if sftp_client is None:
        raise Exception('There was an error establishing a connection to the remote SFTP server')
    for filepath, file in Folder().fileList(export_folder, user, data=False):
        try:
            sftp_one_item(filepath, file, sftp_destination, sftp_client)
        except:
            logger.error(f'There was an error transferring {filepath} to the remote destination')
    sftp_client.close()
    logger.info('SFTP done')


def get_sftp_client():
    """Create an instance of paramiko.SFTPClient based on girder config."""
    host = config.getConfig('sftp_host')
    port = config.getConfig('sftp_port', 22)
    user = config.getConfig('sftp_user')
    password = config.getConfig('sftp_password')

    transport = paramiko.Transport((host, port))
    transport.connect(username=user, password=password)
    sftp_client = paramiko.SFTPClient.from_transport(transport)
    return sftp_client


def sftp_one_item(filepath, file, destination, sftp_client):
    """
    Send a file to a remote server via SFTP.

    :param filepath: the file path of this item
    :param file: the file document of this item
    :param destination: the remote folder for the file to be sent to
    :param sftp_client: an instance of paramiko.SFTPClient
    """
    file_path_segments = filepath.split(os.path.sep)
    image_dir = file_path_segments[-2]
    file_name = file_path_segments[-1]
    full_remote_path = os.path.join(destination, image_dir, file_name)
    item = Item().load(file['itemId'], force=True, exc=False)
    tile_source = ImageItem().tileSource(item)
    tile_source_path = tile_source._getLargeImagePath()

    sftp_client.chdir(destination)
    remote_dirs = sftp_client.listdir()
    if image_dir not in remote_dirs:
        sftp_client.mkdir(image_dir)

    existing_files = sftp_client.listdir(os.path.join(destination, image_dir))
    if file_name in existing_files:
        existing_file_stat = sftp_client.stat(full_remote_path)
        if existing_file_stat.st_size == file['size']:
            logger.info(f'File {file_name} already exists at the remote destination')
            return
    transferred_file_stat = sftp_client.put(tile_source_path, full_remote_path)
    if transferred_file_stat.st_size == file['size']:
        logger.info(f'File {file_name} successfully transferred to remote destination')
    else:
        logger.error(f'There was an error transferring {file_name} to remote destination')


def exportItemsNext(mode, ctx, byteCount, totalByteCount, filepath, file,
                    exportPath, user, report):
    """
    Export an item or report on its size.

    :param mode: either 'measure' or 'copy'.
    :param ctx: a progress context.
    :param byteCount: the number of bytes copies so far.
    :param totalByteCount: the total number of bytes needed to be copies.
    :param filepath: the file path of this item.
    :param file: the file document of this item.
    :param exportPath: the destination for the export.
    :param user: the user triggering this.
    :param report: a collected report list.
    :returns: the number of bytes that are copied.  If mode is measure, no
        copying is actually done.
    """
    from . import __version__

    item = Item().load(file['itemId'], force=True, exc=False)
    try:
        tileSource = ImageItem().tileSource(item)
    except Exception:
        return 0
    sourcePath = tileSource._getLargeImagePath()
    if not all and item.get('meta', {}).get('wsi_deidExported'):
        return 0
    filepath = filepath.split(os.path.sep, 1)[1]
    if mode == 'copy':
        ctx.update(message='Exporting %s' % filepath, total=totalByteCount, current=byteCount)
    destPath = os.path.join(exportPath, filepath)
    destFolder = os.path.dirname(destPath)
    if os.path.exists(destPath):
        if mode == 'copy':
            if os.path.getsize(destPath) == file['size']:
                report.append({'item': item, 'status': 'present'})
            else:
                report.append({'item': item, 'status': 'different'})
        return 0
    else:
        if mode == 'copy':
            os.makedirs(destFolder, exist_ok=True)
            # When run in a docker in Windows, cp is around twice as fast as
            # shutil.copy2 for Python < 3.8.  For Python >=3.8, shutil.copy2 is
            # even slower (by about a factor of 3 from shutil in Python < 3.8),
            # seemingly because the internal call to posix.sendfile is terrible
            # in a linux docker under Windows.
            try:
                subprocess.check_call(['cp', '--preserve=timestamps', sourcePath, destPath])
            except Exception:
                shutil.copy2(sourcePath, destPath)
            exportedRecord = item.get('meta', {}).get('wsi_deidExported', [])
            exportedRecord.append({
                'time': datetime.datetime.utcnow().isoformat(),
                'user': str(user['_id']) if user else None,
                'version': __version__,
            })
            item = Item().setMetadata(item, {'wsi_deidExported': exportedRecord})
            report.append({
                'item': item,
                'status': 'finished',
                'time': exportedRecord[-1]['time'],
            })
        return file['size']


def exportNoteRejected(report, user, all, allFiles=True):
    """
    Note items that are rejected or quarantined, collecting them for a report.

    :param report: a list of items to report.
    :param user: the user triggering this.
    :param all: True to export all items.  False to only export items that have
        not been previously exported.
    :param allFiles: True to report on all files in all folders.  False to only
        report rejected and quarantined items.
    """
    from . import __version__

    shortList = [
        ('rejected', PluginSettings.HUI_REJECTED_FOLDER),
        ('quarantined', PluginSettings.HUI_QUARANTINE_FOLDER),
    ]
    longList = shortList + [
        ('imported', PluginSettings.HUI_INGEST_FOLDER),
        ('processed', PluginSettings.HUI_PROCESSED_FOLDER),
    ]
    for status, settingkey in (shortList if not allFiles else longList):
        folderId = Setting().get(settingkey)
        folder = Folder().load(folderId, force=True, exc=True)
        for _, file in Folder().fileList(folder, user, data=False):
            item = Item().load(file['itemId'], force=True, exc=True)
            try:
                ImageItem().tileSource(item)
            except Exception:
                continue
            if not all and item.get('meta', {}).get('wsi_deidExported'):
                continue
            exportedRecord = item.get('meta', {}).get('wsi_deidExported', [])
            exportedRecord.append({
                'time': datetime.datetime.utcnow().isoformat(),
                'user': str(user['_id']) if user else None,
                'version': __version__,
                'status': status,
            })
            item = Item().setMetadata(item, {'wsi_deidExported': exportedRecord})
            report.append({
                'item': item,
                'status': status,
                'time': exportedRecord[-1]['time'],
            })


def exportReport(ctx, exportPath, report, user):
    """
    Create an export report.

    :param ctx: a progress context.
    :param exportPath: directory for exports
    :param report: a list of files that were exported.
    :param user: the user triggering this.
    :return: the Girder file with the report
    """
    ctx.update(message='Generating report')
    statusDict = {
        'finished': 'Approved',
        'present': 'Approved',
        'redacted': 'Approved',
        'quarantined': 'Quarantined',
        'rejected': 'Rejected',
        'imported': 'AvailableToProcess',
        'processed': 'ReadyForApproval',
        'different': 'FailedToExport',
    }
    curtime = datetime.datetime.utcnow()
    dataList = []
    timeformat = '%m%d%Y: %H%M%S'
    for row in report:
        row['item']['meta'].setdefault('deidUpload', {})
        data = {}
        data.update(row['item']['meta']['deidUpload'])
        data['DSAImageStatus'] = statusDict.get(row['status'], row['status'])
        if data['DSAImageStatus'] == 'Approved':
            data['Date_DEID_Export'] = curtime.strftime(timeformat)
        if data['DSAImageStatus'] != 'AvailableToProcess':
            data['Last_DEID_RunDate'] = row['item'].get(
                'modified', row['item']['created']).strftime(timeformat)
        if 'redacted' in row['item']['meta']:
            try:
                info = row['item']['meta']['redacted'][-1]
                data['ScannerMake'] = info['details']['format'].capitalize()
                data['ScannerModel'] = info['details']['model']
                data['ByteSize_InboundWSI'] = row['item']['meta']['redacted'][0]['originalSize']
                data['ByteSize_ExportedWSI'] = info['redactedSize']
                data['Total_VendorMetadataFields'] = info[
                    'details']['fieldCount']['metadata']['redactable'] + info[
                    'details']['fieldCount']['metadata']['automatic']
                data['Total_VendorMetadataFields_ModifiedOrCreated'] = len(
                    info['redactList']['metadata'])
                data['Automatic_DEID_PHIPII_MetadataFieldsModifiedRedacted'] = ', '.join(sorted(
                    k.rsplit(';', 1)[-1] for k, v in info['redactList']['metadata'].items())
                ) or 'N/A'
                data['Addtl_UserIdentifiedPHIPII_BINARY'] = 'Yes' if (
                    info['details']['redactionCount']['images'] or
                    info['details']['redactionCount']['metadata']) else 'No'
                data['Total_Addtl_UserIdentifiedPHIPII_MetadataFields'] = info[
                    'details']['redactionCount']['metadata']
                data['Addtl_UserIdentifiedPHIPII_MetadataFields'] = ', '.join(sorted(
                    k.rsplit(';', 1)[-1] for k, v in info['redactList']['metadata'].items()
                    if v.get('reason'))) or 'N/A'
                data['Addtl_UserIdentifiedPHIPII_Category_MetadataFields'] = ', '.join(sorted(set(
                    v['category'] for k, v in info['redactList']['metadata'].items()
                    if v.get('reason') and v.get('category')))) or 'N/A'
                data['Addtl_UserIdentifiedPHIPII_DetailedType_MetadataFields'] = ', '.join(sorted(
                    set(
                        v['reason'] for k, v in info['redactList']['metadata'].items()
                        if v.get('reason') and v.get('category') == 'Personal_Info'))) or 'N/A'
                data['Total_VendorImageComponents'] = info[
                    'details']['fieldCount']['images']
                data['Total_UserIdentifiedPHIPII_ImageComponents'] = info[
                    'details']['redactionCount']['images']
                data['UserIdentifiedPHIPII_ImageComponents'] = ', '.join(sorted(
                    k for k, v in info['redactList']['images'].items()
                    if v.get('reason'))) or 'N/A'
                data['UserIdentifiedPHIPII_Category_ImageComponents'] = ', '.join(sorted(set(
                    v['category'] for k, v in info['redactList']['images'].items()
                    if v.get('reason') and v.get('category')))) or 'N/A'
                data['UserIdentifiedPHIPII_DetailedType_ImageComponents'] = ', '.join(sorted(set(
                    v['reason'] for k, v in info['redactList']['images'].items()
                    if v.get('reason') and v.get('category') == 'Personal_Info'))) or 'N/A'
            except KeyError:
                pass
        dataList.append(data)
    df = pd.DataFrame(dataList, columns=[
        'Last_DEID_RunDate', 'Date_DEID_Export',
        'TokenID', 'Proc_Seq', 'Proc_Type', 'Spec_Site', 'Slide_ID', 'ImageID',
        'ScannerMake', 'ScannerModel',
        'DSAImageStatus',
        'ByteSize_InboundWSI',
        'ByteSize_ExportedWSI',
        'Total_VendorMetadataFields',
        'Total_VendorMetadataFields_ModifiedOrCreated',
        # Space separated list of redacted fields names (chunk after last ;)
        'Automatic_DEID_PHIPII_MetadataFieldsModifiedRedacted',
        'Addtl_UserIdentifiedPHIPII_BINARY',          # no/yes
        'Total_Addtl_UserIdentifiedPHIPII_MetadataFields',
        # N/A is none, otherwise space separated field names (chunk after ;)
        'Addtl_UserIdentifiedPHIPII_MetadataFields',
        # category list or N/A
        'Addtl_UserIdentifiedPHIPII_Category_MetadataFields',
        # reason list or N/A
        'Addtl_UserIdentifiedPHIPII_DetailedType_MetadataFields',
        'Total_VendorImageComponents',
        'Total_UserIdentifiedPHIPII_ImageComponents',
        'UserIdentifiedPHIPII_ImageComponents',
        'UserIdentifiedPHIPII_Category_ImageComponents',
        'UserIdentifiedPHIPII_DetailedType_ImageComponents',
    ])
    exportName = 'DeID Export Job %s.xlsx' % datetime.datetime.now().strftime('%Y%m%d %H%M%S')
    reportFolder = 'Export Job Reports'
    path = os.path.join(exportPath, exportName)
    ctx.update(message='Saving report')
    df.to_excel(path, index=False)
    return saveToReports(path, XLSX_MIMETYPE, user, reportFolder)


def saveToReports(path, mimetype=None, user=None, folderName=None):
    """
    Save a file to the reports folder.

    :param path: path of the file to save.
    :param mimetype: the mimetype of the file.
    :param user: the user triggering this.
    :param folderName: if not None, create a folder in the reportsFolder with
        this name and store the new report in that folder.
    :return: the Girder file with the report
    """
    reportsFolderId = Setting().get(PluginSettings.HUI_REPORTS_FOLDER)
    reportsFolder = Folder().load(reportsFolderId, force=True, exc=False)
    if not reportsFolder:
        raise Exception('Reports folder not specified.')
    if folderName:
        reportsFolder = Folder().createFolder(
            reportsFolder, folderName, creator=user, reuseExisting=True)
    with open(path, 'rb') as f:
        file = Upload().uploadFromFile(
            f, size=os.path.getsize(path), name=os.path.basename(path),
            parentType='folder', parent=reportsFolder, user=user,
            mimeType=mimetype)
        return file

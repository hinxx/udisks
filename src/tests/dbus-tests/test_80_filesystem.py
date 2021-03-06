import dbus
import os
import six
import shutil
import tempfile
import unittest

from multiprocessing import Process, Pipe

import gi
gi.require_version('GLib', '2.0')
from gi.repository import GLib

import safe_dbus
import udiskstestcase


class UdisksFSTestCase(udiskstestcase.UdisksTestCase):
    _fs_name = None
    _can_create = False
    _can_label = False
    _can_mount = False

    def _clean_format(self, disk):
        d = dbus.Dictionary(signature='sv')
        d['erase'] = True
        disk.Format('empty', d, dbus_interface=self.iface_prefix + '.Block')

    def _unmount(self, disk_path):
        self.run_command('umount %s' % disk_path)

    @classmethod
    def command_exists(cls, command):
        ret, _out = cls.run_command('type %s' % command)
        return ret == 0

    @classmethod
    def module_loaded(cls, module):
        ret, _out = cls.run_command('lsmod | grep %s' % module)
        return ret == 0

    def test_create_format(self):
        if not self._can_create:
            self.skipTest('Cannot create %s filesystem' % self._fs_name)

        disk = self.get_object('/block_devices/' + os.path.basename(self.vdevs[0]))
        self.assertIsNotNone(disk)

        # create filesystem
        disk.Format(self._fs_name, self.no_options, dbus_interface=self.iface_prefix + '.Block')
        self.addCleanup(self._clean_format, disk)

        # test dbus properties
        usage = self.get_property(disk, '.Block', 'IdUsage')
        usage.assertEqual('filesystem')

        fstype = self.get_property(disk, '.Block', 'IdType')
        fstype.assertEqual(self._fs_name)

        # test system values
        _ret, sys_fstype = self.run_command('lsblk -d -no FSTYPE %s' % self.vdevs[0])
        self.assertEqual(sys_fstype, self._fs_name)

    def _invalid_label(self, disk):
        pass

    def test_label(self):
        if not self._can_create:
            self.skipTest('Cannot create %s filesystem' % self._fs_name)

        if not self._can_label:
            self.skipTest('Cannot set label on %s filesystem' % self._fs_name)

        disk = self.get_object('/block_devices/' + os.path.basename(self.vdevs[0]))
        self.assertIsNotNone(disk)

        # create filesystem with label
        label = 'TEST' if self._fs_name == 'vfat' else 'test'  # XXX mkfs.vfat changes labels to uppercase
        d = dbus.Dictionary(signature='sv')
        d['label'] = label
        disk.Format(self._fs_name, d, dbus_interface=self.iface_prefix + '.Block')
        self.addCleanup(self._clean_format, disk)

        # test dbus properties
        dbus_label = self.get_property(disk, '.Block', 'IdLabel')
        dbus_label.assertEqual(label)

        # test system values
        _ret, sys_label = self.run_command('lsblk -d -no LABEL %s' % self.vdevs[0])
        self.assertEqual(sys_label, label)

        # change the label
        label = 'AAAA' if self._fs_name == 'vfat' else 'aaaa'  # XXX udisks changes vfat labels to uppercase
        disk.SetLabel(label, self.no_options, dbus_interface=self.iface_prefix + '.Filesystem')

        # test dbus properties
        dbus_label = self.get_property(disk, '.Block', 'IdLabel')
        dbus_label.assertEqual(label)

        # test system values
        _ret, sys_label = self.run_command('lsblk -d -no LABEL %s' % self.vdevs[0])
        self.assertEqual(sys_label, label)

        # test invalid label behaviour
        self._invalid_label(disk)

    def test_mount_auto(self):
        if not self._can_create:
            self.skipTest('Cannot create %s filesystem' % self._fs_name)

        if not self._can_mount:
            self.skipTest('Cannot mount %s filesystem' % self._fs_name)

        disk = self.get_object('/block_devices/' + os.path.basename(self.vdevs[0]))
        self.assertIsNotNone(disk)

        # create filesystem
        disk.Format(self._fs_name, self.no_options, dbus_interface=self.iface_prefix + '.Block')
        self.addCleanup(self._clean_format, disk)

        # not mounted
        mounts = self.get_property(disk, '.Filesystem', 'MountPoints')
        mounts.assertLen(0)

        # mount
        d = dbus.Dictionary(signature='sv')
        d['fstype'] = self._fs_name
        d['options'] = 'ro'
        mnt_path = disk.Mount(d, dbus_interface=self.iface_prefix + '.Filesystem')
        self.addCleanup(self._unmount, self.vdevs[0])

        # dbus mountpoint
        dbus_mounts = self.get_property(disk, '.Filesystem', 'MountPoints')
        dbus_mounts.assertLen(1)  # just one mountpoint
        dbus_mnt = self.ay_to_str(dbus_mounts.value[0])  # mountpoints are arrays of bytes
        self.assertEqual(dbus_mnt, mnt_path)

        # system mountpoint
        self.assertTrue(os.path.ismount(mnt_path))
        _ret, out = self.run_command('mount | grep %s' % self.vdevs[0])
        self.assertIn(mnt_path, out)
        self.assertIn('ro', out)

        # umount
        disk.Unmount(self.no_options, dbus_interface=self.iface_prefix + '.Filesystem')
        self.assertFalse(os.path.ismount(mnt_path))

    def test_mount_fstab(self):
        if not self._can_create:
            self.skipTest('Cannot create %s filesystem' % self._fs_name)

        if not self._can_mount:
            self.skipTest('Cannot mount %s filesystem' % self._fs_name)

        # this test will change /etc/fstab, we might want to revert the changes after it finishes
        fstab = self.read_file('/etc/fstab')
        self.addCleanup(self.write_file, '/etc/fstab', fstab)

        disk = self.get_object('/block_devices/' + os.path.basename(self.vdevs[0]))
        self.assertIsNotNone(disk)

        # create filesystem
        disk.Format(self._fs_name, self.no_options, dbus_interface=self.iface_prefix + '.Block')
        self.addCleanup(self._clean_format, disk)

        # create a tempdir
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)

        # configuration items as arrays of dbus.Byte
        mnt = self.str_to_ay(tmp)
        fstype = self.str_to_ay(self._fs_name)
        opts = self.str_to_ay('ro')

        # set the new configuration
        conf = dbus.Dictionary({'dir': mnt, 'type': fstype, 'opts': opts, 'freq': 0, 'passno': 0},
                               signature=dbus.Signature('sv'))
        disk.AddConfigurationItem(('fstab', conf), self.no_options,
                                  dbus_interface=self.iface_prefix + '.Block')

        # mount using fstab options
        disk.Mount(self.no_options, dbus_interface=self.iface_prefix + '.Filesystem')
        self.addCleanup(self._unmount, self.vdevs[0])

        # dbus mountpoint
        dbus_mounts = self.get_property(disk, '.Filesystem', 'MountPoints')
        dbus_mounts.assertLen(1)  # just one mountpoint
        dbus_mnt = self.ay_to_str(dbus_mounts.value[0])  # mountpoints are arrays of bytes
        self.assertEqual(dbus_mnt, tmp)

        # system mountpoint
        self.assertTrue(os.path.ismount(tmp))

        _ret, out = self.run_command('mount | grep %s' % self.vdevs[0])
        self.assertIn(tmp, out)
        self.assertIn('ro', out)


class Ext2TestCase(UdisksFSTestCase):
    _fs_name = 'ext2'
    _can_create = True and UdisksFSTestCase.command_exists('mke2fs')
    _can_label = True and UdisksFSTestCase.command_exists('tune2fs')
    _can_mount = True

    def _invalid_label(self, disk):
        label = 'a' * 17  # at most 16 characters, longer should be truncated
        disk.SetLabel(label, self.no_options, dbus_interface=self.iface_prefix + '.Filesystem')

        # test dbus properties
        dbus_label = self.get_property(disk, '.Block', 'IdLabel')
        dbus_label.assertEqual(label[0:16])

        # test system values
        _ret, sys_label = self.run_command('lsblk -d -no LABEL %s' % self.vdevs[0])
        self.assertEqual(sys_label, label[0:16])


class Ext3TestCase(Ext2TestCase):
    _fs_name = 'ext3'

    def _invalid_label(self, disk):
        pass


class Ext4TestCase(Ext2TestCase):
    _fs_name = 'ext4'

    def _invalid_label(self, disk):
        pass


class XFSTestCase(UdisksFSTestCase):
    _fs_name = 'xfs'
    _can_create = True and UdisksFSTestCase.command_exists('mkfs.xfs')
    _can_label = True and UdisksFSTestCase.command_exists('xfs_admin')
    _can_mount = True

    def _invalid_label(self, disk):
        label = 'a a'  # space not allowed
        msg = 'org.freedesktop.UDisks2.Error.Failed: Error setting label'
        with six.assertRaisesRegex(self, dbus.exceptions.DBusException, msg):
            disk.SetLabel(label, self.no_options, dbus_interface=self.iface_prefix + '.Filesystem')


class VFATTestCase(UdisksFSTestCase):
    _fs_name = 'vfat'
    _can_create = True and UdisksFSTestCase.command_exists('mkfs.vfat')
    _can_label = True and UdisksFSTestCase.command_exists('fatlabel')
    _can_mount = True

    username = 'udisks_mount_test'

    def _invalid_label(self, disk):
        label = 'a' * 12  # at most 11 characters
        msg = 'org.freedesktop.UDisks2.Error.Failed: Error setting label'
        with six.assertRaisesRegex(self, dbus.exceptions.DBusException, msg):
            disk.SetLabel(label, self.no_options, dbus_interface=self.iface_prefix + '.Filesystem')

    def _add_user(self):
        ret, out = self.run_command('useradd -M -p "" %s' % self.username)
        if ret != 0:
            self.fail('Failed to create user %s: %s' % (self.username, out))

        ret, uid = self.run_command('id -u %s' % self.username)
        if ret != 0:
            self.fail('Failed to get UID for user %s' % self.username)

        ret, gid = self.run_command('id -g %s' % self.username)
        if ret != 0:
            self.fail('Failed to get GID for user %s.' % self.username)

        return (uid, gid)

    def _remove_user(self):
        ret, out = self.run_command('userdel %s' % self.username)
        if ret != 0:
            self.fail('Failed to remove user user %s: %s' % (self.username, out))

    def _set_user_mountable(self, disk):
        # create a tempdir
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)

        # configuration items as arrays of dbus.Byte
        mnt = self.str_to_ay(tmp)
        fstype = self.str_to_ay(self._fs_name)
        opts = self.str_to_ay('users,x-udisks-auth')

        # set the new configuration
        conf = dbus.Dictionary({'dir': mnt, 'type': fstype, 'opts': opts, 'freq': 0, 'passno': 0},
                               signature=dbus.Signature('sv'))
        disk.AddConfigurationItem(('fstab', conf), self.no_options,
                                  dbus_interface=self.iface_prefix + '.Block')

    def _mount_as_user_fstab(self, pipe, uid, gid, device):
        """ Try to mount and then unmount @device as user with given @uid and
            @gid.
            @device should be listed in /etc/fstab with proper options so user
            is able to run these operations and this shouldn't fail.
        """
        os.setresgid(gid, gid, gid)
        os.setresuid(uid, uid, uid)

        # try to mount the device
        try:
            safe_dbus.call_sync(self.iface_prefix,
                                self.path_prefix + '/block_devices/' + os.path.basename(device),
                                self.iface_prefix + '.Filesystem',
                                'Mount',
                                GLib.Variant('(a{sv})', ({},)))
        except Exception as e:
            pipe.send([False, 'Mount DBus call failed: %s' % str(e)])
            pipe.close()
            return

        ret, out = self.run_command('grep \"%s\" /proc/mounts' % device)
        if ret != 0:
            pipe.send([False, '%s not mounted' % device])
            pipe.close()
            return

        if 'uid=%s,gid=%s' % (uid, gid) not in out:
            pipe.send([False, '%s not mounted with given uid/gid.\nMount info: %s' % (device, out)])
            pipe.close()
            return

        # and now try to unmount it
        try:
            safe_dbus.call_sync(self.iface_prefix,
                                self.path_prefix + '/block_devices/' + os.path.basename(device),
                                self.iface_prefix + '.Filesystem',
                                'Unmount',
                                GLib.Variant('(a{sv})', ({},)))
        except Exception as e:
            pipe.send([False, 'Unmount DBus call failed: %s' % str(e)])
            pipe.close()
            return

        ret, _out = self.run_command('grep \"%s\" /proc/mounts' % device)
        if ret == 0:
            pipe.send([False, '%s mounted after unmount called' % device])
            pipe.close()
            return

        pipe.send([True, ''])
        pipe.close()
        return

    def _mount_as_user_fstab_fail(self, pipe, uid, gid, device):
        """ Try to mount @device as user with given @uid and @gid.
            @device shouldn't be listed in /etc/fstab when running this, so
            this is expected to fail.
        """
        os.setresgid(gid, gid, gid)
        os.setresuid(uid, uid, uid)

        # try to mount the device -- it should fail
        try:
            safe_dbus.call_sync(self.iface_prefix,
                                self.path_prefix + '/block_devices/' + os.path.basename(device),
                                self.iface_prefix + '.Filesystem',
                                'Mount',
                                GLib.Variant('(a{sv})', ({},)))
        except Exception as e:
            msg = 'GDBus.Error:org.freedesktop.UDisks2.Error.NotAuthorizedCanObtain: Not authorized to perform operation'
            if msg in str(e):
                pipe.send([True, ''])
                pipe.close()
                return
            else:
                pipe.send([False, 'Mount DBus call failed with unexpected exception: %s' % str(e)])
                pipe.close()
                return

        ret, _out = self.run_command('grep \"%s\" /proc/mounts' % device)
        if ret == 0:
            pipe.send([False, '%s was mounted for UID %d without proper record in fstab' % (device, uid)])
            pipe.close()
            return
        else:
            pipe.send([False, 'Mount DBus call didn\'t fail but %s doesn\'t seem to be mounted.' % device])
            pipe.close()
            return

    def _unmount_as_user_fstab_fail(self, pipe, uid, gid, device):
        """ Try to unmount @device as user with given @uid and @gid.
            @device shouldn't be listed in /etc/fstab when running this, so
            this is expected to fail.
        """
        os.setresgid(gid, gid, gid)
        os.setresuid(uid, uid, uid)

        # try to mount the device -- it should fail
        try:
            safe_dbus.call_sync(self.iface_prefix,
                                self.path_prefix + '/block_devices/' + os.path.basename(device),
                                self.iface_prefix + '.Filesystem',
                                'Unmount',
                                GLib.Variant('(a{sv})', ({},)))
        except Exception as e:
            msg = 'GDBus.Error:org.freedesktop.UDisks2.Error.NotAuthorizedCanObtain: Not authorized to perform operation'
            if msg in str(e):
                pipe.send([True, ''])
                pipe.close()
                return
            else:
                pipe.send([False, 'Unmount DBus call failed with unexpected exception: %s' % str(e)])
                pipe.close()
                return

        ret, _out = self.run_command('grep \"%s\" /proc/mounts' % device)
        if ret == 0:
            pipe.send([False, 'Unmount DBus call didn\'t fail but %s seems to be still mounted.' % device])
            pipe.close()
            return
        else:
            pipe.send([False, '%s was unmounted for UID %d without proper record in fstab' % (device, uid)])
            pipe.close()
            return

    @unittest.skipUnless("JENKINS_HOME" in os.environ, "skipping test that modifies system configuration")
    def test_mount_fstab_user(self):
        if not self._can_create:
            self.skipTest('Cannot create %s filesystem' % self._fs_name)

        if not self._can_mount:
            self.skipTest('Cannot mount %s filesystem' % self._fs_name)

        # this test will change /etc/fstab, we might want to revert the changes after it finishes
        fstab = self.read_file('/etc/fstab')
        self.addCleanup(self.write_file, '/etc/fstab', fstab)

        disk = self.get_object('/block_devices/' + os.path.basename(self.vdevs[0]))
        self.assertIsNotNone(disk)

        # create filesystem
        disk.Format(self._fs_name, self.no_options, dbus_interface=self.iface_prefix + '.Block')
        self.addCleanup(self._clean_format, disk)

        # create user for our test
        self.addCleanup(self._remove_user)
        uid, gid = self._add_user()

        # add the disk to fstab
        self._set_user_mountable(disk)

        # create pipe to get error (if any)
        parent_conn, child_conn = Pipe()

        proc = Process(target=self._mount_as_user_fstab, args=(child_conn, int(uid), int(gid), self.vdevs[0]))
        proc.start()
        res = parent_conn.recv()
        parent_conn.close()
        proc.join()

        if not res[0]:
            self.fail(res[1])

    @unittest.skipUnless("JENKINS_HOME" in os.environ, "skipping test that modifies system configuration")
    def test_mount_fstab_user_fail(self):
        if not self._can_create:
            self.skipTest('Cannot create %s filesystem' % self._fs_name)

        if not self._can_mount:
            self.skipTest('Cannot mount %s filesystem' % self._fs_name)

        # this test will change /etc/fstab, we might want to revert the changes after it finishes
        fstab = self.read_file('/etc/fstab')
        self.addCleanup(self.write_file, '/etc/fstab', fstab)

        disk = self.get_object('/block_devices/' + os.path.basename(self.vdevs[0]))
        self.assertIsNotNone(disk)

        # create filesystem
        disk.Format(self._fs_name, self.no_options, dbus_interface=self.iface_prefix + '.Block')
        self.addCleanup(self._clean_format, disk)

        # create user for our test
        self.addCleanup(self._remove_user)
        uid, gid = self._add_user()

        # add unmount cleanup now in case something wrong happens in the other process
        self.addCleanup(self._unmount, self.vdevs[0])

        # create pipe to get error (if any)
        parent_conn, child_conn = Pipe()

        proc = Process(target=self._mount_as_user_fstab_fail, args=(child_conn, int(uid), int(gid), self.vdevs[0]))
        proc.start()
        res = parent_conn.recv()
        parent_conn.close()
        proc.join()

        if not res[0]:
            self.fail(res[1])

        # now mount it as root and test that user can't unmount it
        mnt_path = disk.Mount(self.no_options, dbus_interface=self.iface_prefix + '.Filesystem')
        self.assertIsNotNone(mnt_path)
        self.assertTrue(os.path.ismount(mnt_path))

        # create pipe to get error (if any)
        parent_conn, child_conn = Pipe()

        proc = Process(target=self._unmount_as_user_fstab_fail, args=(child_conn, int(uid), int(gid), self.vdevs[0]))
        proc.start()
        res = parent_conn.recv()
        parent_conn.close()
        proc.join()

        if not res[0]:
            self.fail(res[1])

        self.assertTrue(os.path.ismount(mnt_path))
        self._unmount(mnt_path)


class NTFSTestCase(UdisksFSTestCase):
    _fs_name = 'ntfs'
    _can_create = True and UdisksFSTestCase.command_exists('mkfs.ntfs')
    _can_label = True and UdisksFSTestCase.command_exists('ntfslabel')
    _can_mount = True


class BTRFSTestCase(UdisksFSTestCase):
    _fs_name = 'btrfs'
    _can_create = True and UdisksFSTestCase.command_exists('mkfs.btrfs')
    _can_label = True and UdisksFSTestCase.command_exists('btrfs')
    _can_mount = True


class ReiserFSTestCase(UdisksFSTestCase):
    _fs_name = 'reiserfs'
    _can_create = True and UdisksFSTestCase.command_exists('mkfs.reiserfs')
    _can_label = True and UdisksFSTestCase.command_exists('reiserfstune')
    _can_mount = True


class MinixTestCase(UdisksFSTestCase):
    _fs_name = 'minix'
    _can_create = True and UdisksFSTestCase.command_exists('mkfs.minix')
    _can_label = False
    _can_mount = True and UdisksFSTestCase.module_loaded('minix')


class NILFS2TestCase(UdisksFSTestCase):
    _fs_name = 'nilfs2'
    _can_create = True and UdisksFSTestCase.command_exists('mkfs.nilfs2')
    _can_label = True and UdisksFSTestCase.command_exists('nilfs-tune')
    _can_mount = True and UdisksFSTestCase.module_loaded('nilfs2')


class F2FSTestCase(UdisksFSTestCase):
    _fs_name = 'f2fs'
    _can_create = True and UdisksFSTestCase.command_exists('mkfs.f2fs')
    _can_label = False
    _can_mount = True and UdisksFSTestCase.module_loaded('f2fs')


class FailsystemTestCase(UdisksFSTestCase):
    # test that not supported operations fail 'nicely'

    def test_create_format(self):
        disk = self.get_object('/block_devices/' + os.path.basename(self.vdevs[0]))
        self.assertIsNotNone(disk)

        # try to create some nonexisting filesystem
        msg = 'org.freedesktop.UDisks2.Error.NotSupported: Creation of file system '\
              'type definitely-nonexisting-fs is not supported'
        with six.assertRaisesRegex(self, dbus.exceptions.DBusException, msg):
            disk.Format('definitely-nonexisting-fs', self.no_options,
                        dbus_interface=self.iface_prefix + '.Block')

    def test_label(self):
        # we need some filesystem that doesn't support labels
        fs = MinixTestCase

        if not fs._can_create:
            self.skipTest('Cannot create %s filesystem to test not supported '
                          'labelling.' % fs._fs_name)

        disk = self.get_object('/block_devices/' + os.path.basename(self.vdevs[0]))
        self.assertIsNotNone(disk)

        # try create minix filesystem with label
        label = 'test'
        d = dbus.Dictionary(signature='sv')
        d['label'] = label

        msg = 'org.freedesktop.UDisks2.Error.NotSupported: File system '\
              'type %s does not support labels' % fs._fs_name
        with six.assertRaisesRegex(self, dbus.exceptions.DBusException, msg):
            disk.Format(fs._fs_name, d, dbus_interface=self.iface_prefix + '.Block')

        # create minix filesystem without label and try to set it later
        disk.Format(fs._fs_name, self.no_options, dbus_interface=self.iface_prefix + '.Block')
        self.addCleanup(self._clean_format, disk)

        msg = 'org.freedesktop.UDisks2.Error.NotSupported: Don\'t know how to '\
              'change label on device of type filesystem:%s' % fs._fs_name
        with six.assertRaisesRegex(self, dbus.exceptions.DBusException, msg):
            disk.SetLabel('test', self.no_options, dbus_interface=self.iface_prefix + '.Filesystem')

    def test_mount_auto(self):
        # we need some mountable filesystem, ext4 should do the trick
        fs = Ext4TestCase

        if not fs._can_create:
            self.skipTest('Cannot create %s filesystem to test not supported '
                          'mount options.' % fs._fs_name)

        disk = self.get_object('/block_devices/' + os.path.basename(self.vdevs[0]))
        self.assertIsNotNone(disk)

        disk.Format(fs._fs_name, self.no_options, dbus_interface=self.iface_prefix + '.Block')
        self.addCleanup(self._clean_format, disk)
        self.addCleanup(self._unmount, self.vdevs[0])  # paranoid cleanup

        # wrong fstype
        d = dbus.Dictionary(signature='sv')
        d['fstype'] = 'xfs'

        msg = '[Ww]rong fs type'
        with six.assertRaisesRegex(self, dbus.exceptions.DBusException, msg):
            mnt_path = disk.Mount(d, dbus_interface=self.iface_prefix + '.Filesystem')
            self.assertIsNone(mnt_path)

        # invalid option
        d = dbus.Dictionary(signature='sv')
        d['fstype'] = fs._fs_name
        d['options'] = 'definitely-nonexisting-option'

        msg = 'org.freedesktop.UDisks2.Error.OptionNotPermitted: Mount option '\
              '`definitely-nonexisting-option\' is not allowed'
        with six.assertRaisesRegex(self, dbus.exceptions.DBusException, msg):
            mnt_path = disk.Mount(d, dbus_interface=self.iface_prefix + '.Filesystem')
            self.assertIsNone(mnt_path)

        # should not be mounted -- so lets try to unmount it
        msg = 'org.freedesktop.UDisks2.Error.NotMounted: Device `%s\' is not '\
              'mounted' % self.vdevs[0]
        with six.assertRaisesRegex(self, dbus.exceptions.DBusException, msg):
            disk.Unmount(self.no_options, dbus_interface=self.iface_prefix + '.Filesystem')

    def test_mount_fstab(self):
        pass


del UdisksFSTestCase  # skip UdisksFSTestCase

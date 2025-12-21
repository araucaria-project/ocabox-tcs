import logging
import time
from typing import Dict, List, Tuple
from pyaraucaria.ffs import FFS
from pyaraucaria.fits import save_fits_from_array
import numpy as np

from fits_proc.astro_tools import AstroTools
from fits_proc.iter_async import AsyncListIter, AsyncRangeIter
from fits_proc.modules.abstract_module import AbstractModule
from serverish.messenger.msg_rpc_resp import Rpc
from fits_proc.fits_proc_config import FitsProcConfig as cf
import cv2
import os
from fits_proc.folders import Folders
import math
from fits_proc.images_stacking import ImagesStacking
from scipy.signal import convolve2d


logger = logging.getLogger(__name__.rsplit('.')[-1])


class BaseGuid(AbstractModule):
    def __init__(self, fits_manager: 'FitsManager', module_name: str,
                 module_id: str or None = None) -> None:
        self.arr_shape: Tuple = ()
        self.rpc: Rpc | None = None
        super().__init__(fits_manager=fits_manager, module_name=module_name, module_id=module_id)

    @property
    def guiding_params(self) -> Dict:
        return self.fm.telescope.guiding_params

    @staticmethod
    def gauss_kernel(size, sigma):
        kernel = np.fromfunction(lambda x, y: (1/(2*np.pi*sigma**2))*np.exp(-((x-(size-1)/2)**2+(y-(size-1)/2)**2)/(2*sigma**2)),
                                 (size, size))
        return kernel / np.sum(kernel)

    async def kernel_conv(self, np_array: np.ndarray, fwhm: float = 2.0,  kernel_size: int = 9) -> np.ndarray:
        kernel_sigma = float(fwhm) / 2.355
        np_array = convolve2d(np_array, self.gauss_kernel(size=kernel_size, sigma=kernel_sigma), mode='same')
        return np_array

    async def reduction(self, np_array: np.ndarray) -> Tuple[np.ndarray, str]:
        master_dark_ok = 'error'
        path = os.path.join(Folders.folder_processed(tel_id=self.telescope.id,
                                                     folder_config_name='guiding'), self.master_dark_file_name)
        master_dark = Folders.read_data_from_fits_file(path)
        if master_dark is not None:
            master_dark_ok = 'ok'
            im_st = ImagesStacking(image_sum=1, error_logging_level='debug')
            im_st.master_dark_arr = master_dark['array']
            await im_st.add_image(array=np_array)
            np_array = await im_st.stack()
        # np_array = await self.kernel_conv(np_array=np_array)
        return np_array, master_dark_ok

    @property
    def preview_file_name(self) -> str:
        return 'guider_preview'

    @property
    def rect_file_name(self) -> str:
        return 'guider_rect.jpg'

    @property
    def search_reg_px(self):
        raise NotImplementedError

    @property
    def temp_folder_name(self) -> str:
        return 'temp'

    @property
    def master_dark_file_name(self) -> str:
        a, b = math.modf(self.rpc.data["exp_time"])
        return f'guider_master_dark_{round(b)}_{round(a*10)}.fits'

    def dark_file_name(self, loop: int) -> str:
        a, b = math.modf(self.rpc.data["exp_time"])
        return f'guider_dark_{round(b)}_{round(a*10)}_{loop}_{self.rpc.data["nloops"]}.fits'

    def template_guid_data(self) -> Dict:
        return {'guid_star_pos': None, 'guid_star_adu': None, 'guid_corr': [0, 0], 'arr_shape': self.arr_shape}

    async def array_prep(self, array: List) -> np.ndarray:
        np_array = np.array(array)
        return np_array

    async def find_stars(self, np_array: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        ti = time.time()
        coo, adu = FFS(image=np_array).find_stars(threshold=self.guiding_params['threshold'],
                                                  kernel_size=self.guiding_params['kernel_size'],
                                                  fwhm=self.guiding_params['fwhm'])
        logger.debug(f'Stars pos {coo}')
        logger.debug(f'Stars adu {adu}')
        if len(coo) == 0:
            logger.info(f'No stars found')
        logger.debug(f'Find stars process time:{time.time()-ti}s')
        return coo, adu

    async def guid_star_selection(self, coo: np.ndarray, adu: np.ndarray) -> Dict:
        res = self.template_guid_data()
        try:
            st_sel = self.rpc.data['star_select']
            if len(st_sel) > 1:
                st_sel = st_sel
            else:
                st_sel = None
        except KeyError:
            st_sel = None
        # TODO change to async
        for n in range(coo.shape[0]):
            if st_sel:
                if st_sel[0] + self.search_reg_px > coo[n][0] > st_sel[0] - self.search_reg_px and \
                        st_sel[1] + self.search_reg_px > coo[n][1] > st_sel[1] - self.search_reg_px:
                    res['guid_star_pos'] = coo[n]
                    res['guid_star_adu'] = int(adu[n])
                    logger.info(f'New guiding star coo:{coo[n]} adu:{adu[n]}')
                    return res
            else:
                if self.guiding_params['max_adu'] > adu[n] > self.guiding_params['min_adu']:
                    res['guid_star_pos'] = coo[n]
                    res['guid_star_adu'] = int(adu[n])
                    logger.info(f'New guiding star coo:{coo[n]} adu:{adu[n]}')
                    return res
        logger.info(f'Can not find guiding star')
        return res

    async def calc_correction(self, np_array: np.ndarray, prev_guid_data: Dict) -> Dict | None:
        res = self.template_guid_data()
        if prev_guid_data['guid_star_pos'] is not None:
            pgs_pos = prev_guid_data['guid_star_pos']
            pgs_adu = prev_guid_data['guid_star_adu']
            adu_tol_max = pgs_adu + self.guiding_params['adu_tolerance']
            adu_tol_min = pgs_adu - self.guiding_params['adu_tolerance']
            x_min = pgs_pos[0] - self.guiding_params['search_reg_px']
            x_max = pgs_pos[0] + self.guiding_params['search_reg_px']
            y_min = pgs_pos[1] - self.guiding_params['search_reg_px']
            y_max = pgs_pos[1] + self.guiding_params['search_reg_px']
            if x_min < 0:
                x_min = 0
            if y_min < 0:
                y_min = 0
            if x_max < 0:
                x_max = 0
            if y_max < 0:
                y_max = 0
            coo, adu = await self.find_stars(np_array[y_min:y_max, x_min:x_max])
            coo_in_range = 0
            new_coo_ref = None
            new_adu = None
            # TODO change to async
            for n in range(coo.shape[0]):
                if adu_tol_max > adu[n] > adu_tol_min:
                    new_coo_ref = coo[n]
                    new_adu = adu[n]
                    coo_in_range += 1
            if coo_in_range == 1 and new_coo_ref is not None and new_adu is not None:
                new_coo = np.array([new_coo_ref[0] + x_min, new_coo_ref[1] + y_min])
                res['guid_star_pos'] = new_coo
                res['guid_star_adu'] = new_adu
                res['guid_corr'] =  pgs_pos - new_coo
                logger.info(f'Guiding star coo:{new_coo} adu:{new_adu}')
                logger.info(f"Corr:{res['guid_corr']}")
                return res
            else:
                return None
        else:
            return None

    async def get_prev_guid_data(self, current_fits_id: str) -> Tuple | None:
        prev_id = await self.fm.process_fits.get_fits_id_before(fits_id=current_fits_id)
        prev_guid_data = None
        if prev_id:

            # process_fits Lock
            prev_op_id = await self.fm.process_fits.get_op_id_by_module_name(fits_id=prev_id, module_name='guider')
            # prev_op_id = self.fm.process_fits[prev_id].get_op_id_by_module_name('guider')

            try:

                # process_fits Lock
                prev_guid_data = self.fm.process_fits.get_op_attr(fits_id=prev_id,
                                                                  op_id=prev_op_id, attr='data')['guiding']
                # prev_guid_data = self.fm.process_fits[prev_id].sequence[prev_op_id].data['guiding']

            except (KeyError, IndexError, TypeError):
                logger.debug(f'No guiding data {prev_id}')
        return prev_guid_data, prev_id

    async def find_prev_guid_data(self) -> Dict | None:
        f_id = self.fits_id
        # TODO change to async
        for n in range(0, len(self.fm.process_fits)):
            prev_guid_data, prev_id = await self.get_prev_guid_data(current_fits_id=f_id)
            if prev_guid_data is not None:
                return prev_guid_data
            f_id = prev_id
        return None

    async def save_thumbnail(self, np_array: np.ndarray, file_name: str):
        image_path = os.path.join(Folders.folder_processed(tel_id=self.telescope.id,
                                                           folder_config_name='guiding'), file_name)
        cv2.imwrite(filename=image_path, img=np_array)

    async def save_thumbnails(self, np_array: np.ndarray, guid_data: Dict | None, with_rect: bool = True):
        f = np.copy(np_array)
        f = await AstroTools.image_stretch_display(f, display_max_factor=self.guiding_params['display_max_factor'])
        await self.save_thumbnail(np_array=f, file_name=f'{self.preview_file_name}.jpg')
        if with_rect:
            rect_size = int(guid_data['guid_star_adu'] / 500)
            sp = (guid_data['guid_star_pos'][0] - round(rect_size / 2),
                  guid_data['guid_star_pos'][1] - round(rect_size / 2))
            ep = (guid_data['guid_star_pos'][0] + round(rect_size / 2),
                  guid_data['guid_star_pos'][1] + round(rect_size / 2))
            f = cv2.cvtColor(f, cv2.COLOR_GRAY2BGR)
            f = cv2.rectangle(f, sp, ep, (0, 255, 0), 2)
            await self.save_thumbnail(np_array=f, file_name=self.rect_file_name)

    async def _run(self, array: List):
        raise NotImplementedError

    async def run(self, fits_id: str, op_id: int, **kwargs) -> None:
        self.fits_id = fits_id
        self.op_id = op_id

        # process_fits Lock
        self.rpc = await self.fm.process_fits.get_op_attr(fits_id=self.fits_id, op_id=op_id, attr='rpc')
        # self.rpc = self.fm.process_fits[fits_id].sequence[op_id].rpc

        logger.debug(f'Start download array')
        array = await self.value_proof_get(name='guider_array_get',
                                           awaitab=self.fm.http_conn.get_response,
                                           expect_type=list)
        """
        # +++TEST+++
        file = f'/home/mirk/astro_material/araucaria_material/guider_zb08/im_zb08_8-3s.fits'
        #file = f'/home/mirk/astro_material/araucaria_material/guider_zb08/dark-3s-1.fits'
        #file = f'/home/mirk/astro_material/araucaria_material/guider_zb08/dark-3s-2.fits'
        #file = f'/home/mirk/astro_material/araucaria_material/guider_zb08/dark-3s-3.fits'
        array = np.asarray(Folders.read_data_from_fits_file(file)['array']).tolist()
        # ++++++++++"""

        if array is not None:
            logger.debug(f'Array {self.fits_id} downloaded')
            await self._run(array=array)
            logger.info(f'Module {self.module_name} done, fits_id: {self.fits_id} op_id: {self.op_id},'
                        f' process time:{round(time.time() - self.t_start, 1)}s')

            await self.fm.process_fits.upd_op_status(fits_id=self.fits_id, op_id=self.op_id, new_status='done')
            # self.fm.process_fits[self.fits_id].update_op_status(op_id=self.op_id, new_status='done')
            # self.fm.process_fits[self.fits_id].sequence[self.op_id].progress = 100
        else:
            logger.error(f'Unable to get array from {self.telescope.id}')
            await self.fm.nats_conn.rpc_response(response='no_array', status='error', rpc=self.rpc)
            await self.fm.nats_conn.journ_pub.error('Unable to get array.')

            # process_fits Lock
            await self.fm.process_fits.upd_op_status(fits_id=self.fits_id, op_id=self.op_id, new_status='error')
            # self.fm.process_fits[self.fits_id].update_op_status(op_id=self.op_id, new_status='error')
            # self.fm.process_fits[self.fits_id].update_fits_status(new_status='error')


class GuidSimple(BaseGuid):

    @property
    def search_reg_px(self) -> int:
        return self.guiding_params['search_reg_px']

    async def _run(self, array: List):

        np_array = await self.array_prep(array=array)
        save_fits_from_array(array=array,
                             folder=Folders.folder_processed(tel_id=self.telescope.id,
                                                             folder_config_name='guiding'),
                             file_name=f'oryg_{self.preview_file_name}.fits',
                             header={self.hdr_names["sequence_id"]: self.rpc.data['sequence_id'],
                                     f'{self.hdr_names["exp_time"]}': self.rpc.data['exp_time'],
                                     f'{self.hdr_names["loop"]}': self.rpc.data['loop'],
                                     f'{self.hdr_names["nloops"]}': self.rpc.data['nloops']},
                             overwrite=True,
                             dtyp=self.rpc.data['dtyp'])
        np_array, master_dark_ok = await self.reduction(np_array=np_array)
        self.arr_shape = tuple(np_array.shape)
        prev_guid_data = await self.find_prev_guid_data()
        if prev_guid_data is not None:
            guid_data = await self.calc_correction(np_array=np_array, prev_guid_data=prev_guid_data)
            if guid_data is None:
                await self.save_thumbnails(np_array=np_array, guid_data=None, with_rect=False)
                await self.fm.nats_conn.rpc_response(response='star_lost', status='ok', rpc=self.rpc)
                await self.fm.nats_conn.journ_pub.notice('Guiding star lost.')

                # process_fits Lock
                await self.fm.process_fits.set_op_attr(fits_id=self.fits_id, op_id=self.op_id, attr='data',
                                                       val={'guiding': None})
                # self.fm.process_fits[self.fits_id].sequence[self.op_id].data = {'guiding': None}

            else:
                guid_data['master_dark_ok'] = master_dark_ok
                await self.save_thumbnails(np_array=np_array, guid_data=guid_data)
                await self.fm.nats_conn.rpc_response(
                    response='corr_calc',
                    status='ok',
                    param=await self.data_numpy_to_list(guid_data),
                    rpc=self.rpc
                )
                await self.fm.nats_conn.journ_pub.info('Guider correction calculated.')

                # process_fits Lock
                await self.fm.process_fits.set_op_attr(fits_id=self.fits_id, op_id=self.op_id, attr='data',
                                                       val={'guiding': guid_data})
                # self.fm.process_fits[self.fits_id].sequence[self.op_id].data = {'guiding': guid_data}

        else:
            coo, adu = await self.find_stars(np_array=np_array)
            guid_data = await self.guid_star_selection(coo=coo, adu=adu)
            if len(adu) > 0 and guid_data['guid_star_pos'] is not None:
                await self.save_thumbnails(np_array=np_array, guid_data=guid_data)
                guid_data['master_dark_ok'] = master_dark_ok

                # process_fits Lock
                await self.fm.process_fits.set_op_attr(fits_id=self.fits_id, op_id=self.op_id, attr='data',
                                                       val={'guiding': guid_data})
                # self.fm.process_fits[self.fits_id].sequence[self.op_id].data = {'guiding': guid_data}

                await self.fm.nats_conn.rpc_response(
                    response='star_selected',
                    status='ok',
                    param=await self.data_numpy_to_list(guid_data),
                    rpc=self.rpc
                )
                await self.fm.nats_conn.journ_pub.info('Guider star is selected.')
            else:
                await self.save_thumbnails(np_array=np_array, guid_data=None, with_rect=False)
                await self.fm.nats_conn.rpc_response(response='no_stars', status='ok', rpc=self.rpc)
                await self.fm.nats_conn.journ_pub.notice('Can not find any stars.')


class GuidStack(BaseGuid):
    pass


class GuidDiff(BaseGuid):
    pass


class GuidDark(BaseGuid):

    async def _run(self, array: List):
        guid_folder_path = Folders.folder_processed(tel_id=self.telescope.id,
                                                    folder_config_name='guiding')
        temp_folder_path = os.path.join(guid_folder_path, self.temp_folder_name)
        if not Folders.folder_exist(temp_folder_path):
            Folders.mk_folder(temp_folder_path)
        save_fits_from_array(array=array,
                             folder=temp_folder_path,
                             file_name=self.dark_file_name(self.rpc.data['loop']),
                             header={f'{self.hdr_names["sequence_id"]}': (self.rpc.data['sequence_id'], ),
                                     f'{self.hdr_names["exp_time"]}': (self.rpc.data['exp_time'], ),
                                     f'{self.hdr_names["loop"]}': (self.rpc.data['loop'], ),
                                     f'{self.hdr_names["nloops"]}': (self.rpc.data['nloops'], )},
                             overwrite=True,
                             dtyp=self.rpc.data['dtyp'])
        if self.rpc.data['loop'] != self.rpc.data['nloops']:
            await self.fm.nats_conn.rpc_response(response='dark_saved', status='ok', rpc=self.rpc)
            await self.fm.nats_conn.journ_pub.info('Guider dark saved.')
        else:
            np_array = await self.array_prep(array=array)
            stack = ImagesStacking(image_sum=self.rpc.data['nloops'])
            await stack.add_image(np_array)
            # TODO change to async
            for n in range(1, self.rpc.data['nloops']):
                f = self.dark_file_name(n)
                dat = Folders.read_data_from_fits_file(os.path.join(temp_folder_path, f))
                if dat['header'][self.hdr_names["sequence_id"]] == self.rpc.data['sequence_id'] and \
                    dat['header'][self.hdr_names["exp_time"]] == self.rpc.data['exp_time'] and \
                        dat['header'][self.hdr_names["loop"]] == n and \
                            dat['header'][self.hdr_names["nloops"]] == self.rpc.data['nloops']:
                    await stack.add_image(dat['array'])
                else:
                    logger.error('Master dark stacking error')
                    await self.fm.nats_conn.rpc_response(response='master_dark_stack_error',
                                                   status='error',
                                                   rpc=self.rpc)
                    await self.fm.nats_conn.journ_pub.warning('Master dark stacking error.')
                    return
            await stack.stack()
            save_fits_from_array(array=stack.stacked_array.tolist(),
                                 folder=guid_folder_path,
                                 file_name=self.master_dark_file_name,
                                 header={f'{self.hdr_names["exp_time"]}': self.rpc.data["exp_time"],
                                         f'{self.hdr_names["nloops"]}': self.rpc.data["nloops"]},
                                 overwrite=True,
                                 dtyp=self.rpc.data['dtyp'])
            await self.fm.nats_conn.rpc_response(response='master_dark_saved', status='ok', rpc=self.rpc)
            await self.fm.nats_conn.journ_pub.info(f'Master dark {self.rpc.data["exp_time"]}s saved')
            if self.guiding_params['remove_dark_sub_fits'] == 1:
                if Folders.folder_exist(temp_folder_path):
                    async for k in AsyncListIter(os.listdir(temp_folder_path)):
                        os.remove(os.path.join(temp_folder_path, k))


class GuidCalib(GuidSimple):

    @property
    def search_reg_px(self) -> int:
        return self.guiding_params['search_reg_px'] * 2


class GuidPreview(BaseGuid):

    async def _run(self, array: List):
        np_array = await self.array_prep(array=array)
        save_fits_from_array(array=array,
                             folder=Folders.folder_processed(tel_id=self.telescope.id,
                                                             folder_config_name='guiding'),
                             file_name=f'oryg_{self.preview_file_name}.fits',
                             header={self.hdr_names["sequence_id"]: self.rpc.data['sequence_id'],
                                     f'{self.hdr_names["exp_time"]}': self.rpc.data['exp_time'],
                                     f'{self.hdr_names["loop"]}': self.rpc.data['loop'],
                                     f'{self.hdr_names["nloops"]}': self.rpc.data['nloops']},
                             overwrite=True,
                             dtyp=self.rpc.data['dtyp'])
        np_array, master_dark_ok = await self.reduction(np_array=np_array)
        self.arr_shape = tuple(np_array.shape)
        f = await AstroTools.image_stretch_display(np_array)
        await self.save_thumbnail(np_array=f, file_name=f'{self.preview_file_name}.jpg')
        await self.fm.nats_conn.rpc_response(response='preview_done',
                                status='ok',
                                param={'arr_shape': self.arr_shape, 'master_dark_ok': master_dark_ok},
                                rpc=self.rpc)

class Guider(AbstractModule):
    """
    This is main guiding class. This class distributing guiding rpc request to run guiding submodule.
    """

    DATA_KEYS = ['telescope_id', 'ts', 'fits_id', 'request', 'sequence_id', 'exp_time', 'loop',
                 'nloops', 'tel_ra', 'tel_dec', 'tel_alt', 'tel_az','star_select', 'dtyp']

    REQUESTS = {'guiding_simple': GuidSimple,
                #'guiding_stack': GuidStack, # for future implementation
                #'guiding_diff': GuidDiff, # for future implementation
                'dark': GuidDark,
                'calib': GuidCalib,
                'preview': GuidPreview
                }

    def __init__(self, fits_manager: 'FitsManager', module_name: str = 'guider', **kwargs) -> None:

        self.processing = False
        super().__init__(fits_manager=fits_manager, module_name=module_name)
        self.cam_sim()

    def cam_sim(self) -> None:
        cs = cf.telescope_config(telescope_id=self.telescope.id, config_name='cam_sim')
        if cs != '':
            self.fm.http_conn.cam_sim = True
            self.fm.http_conn.set_address(cs)
            logger.info(f'cam_sim on {cs}')
        else:
            add = self.telescope.guider_array_address
            self.fm.http_conn.set_address(add)
            logger.info(f'Http set address: {add}')

    async def _run(self, fits_id: str, op_id: int, **kwargs) -> None:

        # process_fits Lock
        await self.fm.process_fits.upd_op_status(fits_id=self.fits_id, op_id=self.op_id, new_status='processing')
        # self.fm.process_fits[fits_id].update_op_status(op_id=op_id, new_status='processing')
        # self.fm.process_fits[fits_id].update_fits_status(new_status='processing')

        logger.info(f'Module {self.module_name} started processing: {fits_id} op_id: {op_id}')

        # process_fits Lock
        rpc = await self.fm.process_fits.get_op_attr(fits_id=self.fits_id, op_id=op_id, attr='rpc')
        # rpc = self.fm.process_fits[fits_id].sequence[op_id].rpc

        if await self.validate_data(data=rpc.data, data_keys=self.DATA_KEYS):
            try:
                req = Guider.REQUESTS[rpc.data['request']]
            except KeyError:
                logger.error(f'Wrong request type')
                await self.fm.nats_conn.journ_pub.error(f'Wrong guiding request type')
                await self.fm.nats_conn.rpc_response(response='wrong_request_type', status='error', rpc=rpc)

                # process_fits Lock
                await self.fm.process_fits.upd_op_status(fits_id=self.fits_id, op_id=self.op_id, new_status='error')
                # self.fm.process_fits[fits_id].update_op_status(op_id=op_id, new_status='error')
                # self.fm.process_fits[fits_id].update_fits_status(new_status='error')

                return
            await req(fits_manager=self.fm,
                      module_name=self.module_name).run(fits_id=fits_id, op_id=op_id, **kwargs)
        else:
            logger.error(f'Wrong msg format')
            await self.fm.nats_conn.journ_pub.error(f'Wrong guiding msg format.')
            await self.fm.nats_conn.rpc_response(response='wrong_msg_format', status='error', rpc=rpc)

            # process_fits Lock
            await self.fm.process_fits.upd_op_status(fits_id=self.fits_id, op_id=self.op_id, new_status='error')
            # self.fm.process_fits[fits_id].update_op_status(op_id=op_id, new_status='error')
            # self.fm.process_fits[fits_id].update_fits_status(new_status='error')

    async def run(self, fits_id: str, op_id: int, **kwargs) -> None:
        self.processing = True
        self.t_start = time.time()
        # TODO change for fits_id and op_id
        # self.fits_id = fits_id
        # self.op_id = op_id
        await self._run(fits_id=fits_id, op_id=op_id, **kwargs)
        self.processing = False

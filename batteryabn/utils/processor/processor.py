import pandas as pd 
import numpy as np
import rfcnt

from scipy import integrate, interpolate
from scipy.signal import find_peaks, medfilt, savgol_filter
from scipy.optimize import Bounds, NonlinearConstraint, minimize

from batteryabn import logger, Utils 
from batteryabn import Constants as Const
from batteryabn.models import TestRecord


class Processor:

    def __init__(self) -> None:
        """
        A class to process battery test data.
        """
        self.cell_data = pd.DataFrame(dtype=object)
        self.cell_cycle_metrics = pd.DataFrame(dtype=object)
        self.cell_data_vdf = pd.DataFrame(dtype=object)
        self.cell_rpt_data = pd.DataFrame(dtype=object)
        self.update = False

    def set_processed_data(self, data: pd.DataFrame = None, 
                           cycle_metrics: pd.DataFrame = None, data_vdf: pd.DataFrame =None) -> None:
        """
        Set processed battery test data.

        Parameters
        ----------
        data : pd.DataFrame, optional
            Processed battery test data

        cycle_metrics : pd.DataFrame, optional
            Processed battery test cycle metrics
        
        data_vdf : pd.DataFrame, optional
            Processed battery test voltage data
        """
        self.cell_data = data
        self.cell_cycle_metrics = cycle_metrics
        self.cell_data_vdf = data_vdf

    def process(self, cycler_trs: list[TestRecord], vdf_trs: list[TestRecord] = None, project: str = None):
        """
        Process battery test data.

        Parameters
        ----------
        cycler_trs : list[TestRecord]
            List of TestRecord objects for cycler data

        vdf_trs : list[TestRecord], optional
            List of TestRecord objects for voltage data
        """

        # Process cycler data
        cell_data, cell_cycle_metrics = self.process_cycler_data(cycler_trs, project)

        cell_data_vdf, cell_cycle_metrics = self.process_cycler_expansion(vdf_trs, cell_cycle_metrics, project)    

        # Rearrange columns of cell_cycle_metrics for easy reading with data on left and others on right
        cols = cell_cycle_metrics.columns.to_list()
        move_idxs = [c for c in cols if '[' in c] + [c for c in cols if '[' not in c] 
        cell_cycle_metrics = cell_cycle_metrics[move_idxs]

        self.set_processed_data(cell_data, cell_cycle_metrics, cell_data_vdf)

        self.summarize_rpt_data()

#-----------------Cycler Expansion Processing-----------------#

    def process_cycler_expansion(self, trs: list[TestRecord], cell_cycle_metrics: pd.DataFrame, project: str):
        """
        Process cycler expansion data.

        Parameters
        ----------
        trs : list[TestRecord]
            List of TestRecord objects
        cell_cycle_metrics : pd.DataFrame
            Cycler cycle metrics
        project : str
            Project name

        Returns
        -------
        pd.DataFrame
            Processed cycler expansion data
        pd.DataFrame
            Processed cycler cycle metrics
        """
        cell_data_vdf = self.combine_cycler_expansion_data(trs, project)

        # Find matching cycle timestamps from cycler data
        t_vdf, exp_vdf, exp_vdf_um = (cell_data_vdf[key] for key in [Const.TIME, Const.EXPANSION, Const.EXPANSION_UM])
        cycle_timestamps = cell_cycle_metrics[Const.TIME][cell_cycle_metrics[Const.CYCLE_IDC]]
        t_cycle_vdf, cycle_vdf_idxs, matched_timestamp_idxs = self.find_matching_timestamp(cycle_timestamps, t_vdf)  

        # Add cycle indicator, align with cycles timestamps previously defined by cycler data
        Utils.add_column(cell_data_vdf, Const.CYCLE_IDC, False)
        Utils.set_value(cell_data_vdf, Const.CYCLE_IDC, cycle_vdf_idxs, True)

        # Add additional columns to cell_cycle_metrics
        for column in Const.CCM_COLUMNS_ADDITIONAL_VDF:
            Utils.add_column(cell_cycle_metrics, column, np.nan) 

        # Find min/max expansion and reversible expansion
        cycle_vdf_minmax_idxs = [i for i in cycle_vdf_idxs if i is not np.nan]
        cycle_vdf_minmax_idxs.append(len(t_vdf) - 1) #append end
        exp_max, exp_min = self.max_min_cycle_data(exp_vdf, cycle_vdf_minmax_idxs)
        exp_rev = np.subtract(exp_max, exp_min)
        exp_max_um, exp_min_um = self.max_min_cycle_data(exp_vdf_um, cycle_vdf_minmax_idxs)
        exp_rev_um = np.subtract(exp_max_um, exp_min_um)
        discharge_cycle_idxs = list(np.where(cell_cycle_metrics[Const.CYCLE_IDC])[0])

        # Put the calculated values into the cell_cycle_metrics dataframe
        for i, j in enumerate(matched_timestamp_idxs):
            index = discharge_cycle_idxs[j]
            cell_cycle_metrics.loc[index, Const.TIME_VDF] = t_cycle_vdf[i]
            cell_cycle_metrics.loc[index, Const.MIN_CYCLE_EXPANSION] = exp_min[i]
            cell_cycle_metrics.loc[index, Const.MAX_CYCLE_EXPANSION] = exp_max[i]
            cell_cycle_metrics.loc[index, Const.REV_CYCLE_EXPANSION] = exp_rev[i]
            cell_cycle_metrics.loc[index, Const.MIN_CYCLE_EXPANSION_UM] = exp_min_um[i]
            cell_cycle_metrics.loc[index, Const.MAX_CYCLE_EXPANSION_UM] = exp_max_um[i]
            cell_cycle_metrics.loc[index, Const.REV_CYCLE_EXPANSION_UM] = exp_rev_um[i]
            cell_cycle_metrics.loc[index, Const.DRIVE_CURRENT] = cell_data_vdf.get(Const.DRIVE_CURRENT, [0])[i] if Const.DRIVE_CURRENT in cell_data_vdf else 0
            cell_cycle_metrics.loc[index, Const.EXPANSION_STDDEV] = cell_data_vdf.get(Const.EXPANSION_STDDEV, [0])[i] if Const.EXPANSION_STDDEV in cell_data_vdf else 0
            cell_cycle_metrics.loc[index, Const.REF_STDDEV] = cell_data_vdf.get(Const.REF_STDDEV, [0])[i] if Const.REF_STDDEV in cell_data_vdf else 0

        # Add timestamps for charge cycles
        charge_cycle_idxs = list(np.where(cell_cycle_metrics[Const.CHARGE_CYCLE_IDC])[0])
        charge_cycle_timestamps = cell_cycle_metrics[Const.TIME][cell_cycle_metrics[Const.CHARGE_CYCLE_IDC]]
        t_charge_cycle_vdf, _, matched_charge_timestamp_idx = self.find_matching_timestamp(charge_cycle_timestamps, t_vdf)
        for i, j in enumerate(matched_charge_timestamp_idx):
            cell_cycle_metrics.loc[charge_cycle_idxs[j], Const.TIME_VDF] = t_charge_cycle_vdf[i]

        return cell_data_vdf, cell_cycle_metrics        

    def combine_cycler_expansion_data(self, trs: list[TestRecord], project: str):
        """
        Combine cycler expansion data from multiple files into a single dataframe.
        It will calculate the min, max, and reversible expansion for each cycle. 

        Parameters
        ----------
        trs : list[TestRecord]
            List of TestRecord objects
        project : str
            Project name

        Returns
        -------
        pd.DataFrame
            Combined cycler expansion data
        """
        dfs = []
        for tr in trs:
            dfs.append(self.process_cycler_expansion_tr(tr))
        logger.info(f"Combining {len(dfs)} dataframes")

        if len(dfs) == 0:
            logger.debug("No cycler expansion data found")
            return pd.DataFrame(columns = Const.VDF_COLUMNS)
        
        cell_data_vdf = pd.concat(dfs, ignore_index=True).sort_values(by=Const.TIME)

        return cell_data_vdf
    
    def process_cycler_expansion_tr(self, tr: TestRecord):
        """
        Process and format cycler expansion data for a single test record.

        Parameters
        ----------
        tr : TestRecord
            TestRecord object

        Returns
        -------
        pd.DataFrame
            Processed cycler expansion data
        """
        logger.debug(f"Processing cycler expansion data for {tr.test_name}")
        df = tr.get_test_data()
        df = df[(df[Const.EXPANSION] >1e1) & (df[Const.EXPANSION] <1e7)] #keep good signals 

        x1, x2, c = tr.get_calibration_parameters()
        df[Const.EXPANSION_UM] = 1000 * (30.6 - (x2 * (df[Const.EXPANSION] / 10**6)**2 + x1 * (df[Const.EXPANSION] / 10**6) + c))
        df[Const.TEMPERATURE] = np.where((df[Const.TEMPERATURE] >= 200) & (df[Const.TEMPERATURE] <250), np.nan, df[Const.TEMPERATURE]) 

        return df


    def find_matching_timestamp(self, desired_timestamps, time_series: pd.Series, t_threshold: int = 10000):
        """
        Find the matching timestamps 

        Parameters
        ----------
        desired_timestamps : pd.Series
            Desired timestamps
        time_series : pd.Series
            Time series
        t_threshold : int, optional
            Timestamp threshold

        Returns
        -------
        pd.Series   
            Matched timestamps
        np.ndarray
            Matched indices
        np.ndarray
            Desired timestamp indices
        """
        # Remove duplicates from time_data and create a new series with unique timestamps as index
        unique_time_data = time_series.drop_duplicates().reset_index(drop=True)
        unique_time_data.index = unique_time_data.values

        # Find the indices of the closest match in unique_time_data for each desired timestamp
        desired_timestamp_idxs = np.argwhere(unique_time_data.index.get_indexer(
            desired_timestamps, method="nearest", tolerance=t_threshold * 1000) != -1)[:, 0]
        matched_indices_in_unique = unique_time_data.index.get_indexer(
            desired_timestamps, method="nearest", tolerance=t_threshold * 1000)
        matched_indices_in_unique = matched_indices_in_unique[matched_indices_in_unique != -1]

        # Get the actual matched timestamps and their indices in the original time_data
        matched_timestamps = unique_time_data.iloc[matched_indices_in_unique].index
        matched_idxs = time_series.index.get_indexer_for(matched_timestamps)

        return matched_timestamps, matched_idxs, desired_timestamp_idxs


#-----------------Cycler Data Processing-----------------#

    def process_cycler_data(self, trs: list[TestRecord], project: str):
        """
        Process cycler data.

        Parameters
        ----------
        trs : list[TestRecord]
            List of TestRecord objects
        project : str
            Project name

        Returns
        -------
        pd.DataFrame
            Processed cycler data
        pd.DataFrame
            Processed cycler cycle metrics
        """
        cell_data, cell_cycle_metrics = self.combine_cycler_data(trs)

        #TODO: Put project settings in json file and change this load method
        if project is None or project not in Const.PROJECTS_SETTING.keys():
            project = 'DEFAULT'
        qmax = Const.PROJECTS_SETTING[project]['Qmax']

        charge_t_idxs = list(cell_data[cell_data[Const.CHARGE_CYCLE_IDC]].index)
        discharge_t_idxs = list(cell_data[cell_data[Const.DISCHARGE_CYCLE_IDC]].index)
        q_c, q_d = self.calc_capacities(cell_data[Const.TIME], cell_data[Const.AHT], charge_t_idxs, discharge_t_idxs, qmax)
        i_avg_c, i_avg_d = self.calc_avg_cycle_data(cell_data[Const.TIME], cell_data[Const.CURRENT], charge_t_idxs, discharge_t_idxs)
        
        # Find min/max metrics
        cycle_minmax_idxs = list(cell_data[cell_data[Const.CYCLE_IDC]].index)
        cycle_minmax_idxs.append(len(cell_data)-1)
        v_max, v_min = self.max_min_cycle_data(cell_data[Const.VOLTAGE], cycle_minmax_idxs)
        temp_max, temp_min = self.max_min_cycle_data(cell_data[Const.TEMPERATURE], cycle_minmax_idxs)

        # Add additional columns to cell_cycle_metrics
        for column in Const.CCM_COLUMNS_ADDITIONAL:
            Utils.add_column(cell_cycle_metrics, column, np.nan)

        # Put the calculated values into the cell_cycle_metrics dataframe
        charge_cycle_idxs = list(cell_cycle_metrics[cell_cycle_metrics[Const.CHARGE_CYCLE_IDC]].index)
        discharge_cycle_idxs = list(cell_cycle_metrics[cell_cycle_metrics[Const.DISCHARGE_CYCLE_IDC]].index) 
        cycle_idxs = list(cell_cycle_metrics[cell_cycle_metrics[Const.CYCLE_IDC]].index) # align with charge start
        for i,j in enumerate(charge_cycle_idxs): 
            cell_cycle_metrics.loc[j, Const.CHARGE_CAPACITY] = q_c[i]
            cell_cycle_metrics.loc[j, Const.AVG_CYCLE_CHARGE_CURRENT] = i_avg_c[i]  
        for i,j in enumerate(discharge_cycle_idxs): 
            cell_cycle_metrics.loc[j, Const.DISCHARGE_CAPACITY] = q_d[i]
            cell_cycle_metrics.loc[j, Const.AVG_CYCLE_DISCHARGE_CURRENT] = i_avg_d[i]  
        for i,j in enumerate(cycle_idxs): 
            cell_cycle_metrics.loc[j, Const.MIN_CYCLE_VOLTAGE] = v_min[i] 
            cell_cycle_metrics.loc[j, Const.MAX_CYCLE_VOLTAGE] = v_max[i] 
            cell_cycle_metrics.loc[j, Const.MIN_CYCLE_TEMP] = temp_min[i] 
            cell_cycle_metrics.loc[j, Const.MAX_CYCLE_TEMP] = temp_max[i] 

        return cell_data, cell_cycle_metrics

    def combine_cycler_data(self, trs: list[TestRecord]):
        """
        Combine cycler data from multiple files into a single dataframe.
        Concatenate data frames, identify cycles based on rests (I=0), then calculate min and max voltage and temperature for each cycle.

        Parameters
        ----------
        trs : list[TestRecord]
            List of TestRecord objects

        Returns
        -------
        pd.DataFrame
            Combined cycler data
        pd.DataFrame
            Cycler cycle metrics
        """
        dfs =[]
        for tr in trs:
            dfs.append(self.process_cycle_tr(tr))
        logger.info(f"Combining {len(dfs)} dataframes")
        cell_data = pd.concat(dfs, ignore_index=True)

        discharge_start_idxs_0 = np.where(cell_data[Const.DISCHARGE_CYCLE_IDC])[0]
        charge_start_idxs_0 = np.where(cell_data[Const.CHARGE_CYCLE_IDC])[0]
        capacity_check_idxs_0 = np.where(cell_data[Const.CAPACITY_CHECK_IDC])[0]
        if len(discharge_start_idxs_0) > 1 and len(charge_start_idxs_0) > 1:
            charge_start_idx, discharge_start_idx = self.match_charge_discharge(charge_start_idxs_0, discharge_start_idxs_0)
            removed_charge_cycle_idx = list(set(charge_start_idxs_0).symmetric_difference(set(charge_start_idx)))
            cell_data.loc[removed_charge_cycle_idx, Const.CAPACITY_CHECK_IDC] = False
            removed_discharge_cycle_idx = list(set(discharge_start_idxs_0).symmetric_difference(set(discharge_start_idx)))
            cell_data.loc[removed_discharge_cycle_idx, Const.DISCHARGE_CYCLE_IDC] = False
            removed_capacity_check_idx = list(set(capacity_check_idxs_0).symmetric_difference(set(charge_start_idx)))
            cell_data.loc[removed_capacity_check_idx, Const.CAPACITY_CHECK_IDC] = False

        cell_data[Const.CYCLE_IDC] = cell_data[Const.CHARGE_CYCLE_IDC] #default cycle indicator on charge

        # Save cycle metrics to separate dataframe and sort. Only keep columns where charge and discharge cycles start. Label the type of protocol
        cell_cycle_metrics = cell_data[Const.CCM_COLUMNS][(cell_data[Const.CHARGE_CYCLE_IDC]) | (cell_data[Const.DISCHARGE_CYCLE_IDC])].copy()
        cell_cycle_metrics.reset_index(drop=True, inplace=True)
        logger.info(f"Found {len(cell_data)} cell data, {len(cell_cycle_metrics)} cycles")

        return cell_data, cell_cycle_metrics
            
    def process_cycle_tr(self, tr: TestRecord):
        """
        Process and format cycle data for a single test record.

        Parameters
        ----------
        tr : TestRecord
            TestRecord object

        Returns
        -------
        pd.DataFrame
            Formatted cycle data
        """
        logger.debug(f"Processing cycle data for {tr.test_name}")
        df = tr.get_test_data()
    
        # Get the data
        t = df[Const.TIME].reset_index(drop=True)
        i = df[Const.CURRENT].reset_index(drop=True)
        protocal = tr.get_cycle_type()
        lims = Const.CYCLE_ID_LIMS[protocal]

        # These parameters could be used to filter cycle index in the future
        v = df[Const.VOLTAGE].reset_index(drop=True)
        step_idxs = df[Const.STEP_IDX].reset_index(drop=True)
        cycle_idxs = df[Const.CYCLE_IDX].reset_index(drop=True)
        v_max_cycle, v_min_cycle, dah_min, dt_min = (
            lims[key] for key in (
                Const.V_MAX_CYCLE, 
                Const.V_MIN_CYCLE, 
                Const.DAH_MIN, 
                Const.DT_MIN
            )
        )

        charge_start_idxs, discharge_start_idxs = self.find_cycle_idxs(t, i)
        try: # won't work for half cycles (files with only charge or only discharge)
            charge_start_idxs, discharge_start_idxs = self.match_charge_discharge(charge_start_idxs, discharge_start_idxs)
        except:
            logger.error(f"Error processing {tr.test_name}: failed to match charge discharge")
            pass

        # Add aux cycle indicators to df. Column of True if start of a cycle, otherwise False. 
        # Set default cycle indicator = charge start 
        for indicator in Const.INDICATORS:
            Utils.add_column(df, indicator, False)
        Utils.set_value(df, Const.CHARGE_CYCLE_IDC, charge_start_idxs, True)
        Utils.set_value(df, Const.DISCHARGE_CYCLE_IDC, discharge_start_idxs, True)
        #TODO: test_data['cycle_indicator'] = test_data['charge_cycle_indicator']
        #TODO: is_rpt() and is_format() are not implemented
        is_cap_check = tr.is_rpt() or tr.is_format()
        if is_cap_check:
            Utils.set_value(df, Const.CAPACITY_CHECK_IDC, charge_start_idxs, True)

        # Add cycle type and test name to df
        Utils.add_column(df, Const.CYCLE_TYPE, ' ')
        Utils.add_column(df, Const.TEST_NAME, ' ')
        
        Utils.set_value(df, Const.CYCLE_TYPE, np.concatenate((discharge_start_idxs,charge_start_idxs)), protocal)
        Utils.set_value(df, Const.TEST_NAME, np.concatenate((discharge_start_idxs,charge_start_idxs)), tr.test_name)

        # Identify subcycle type. For extracting HPPC and C/20 dis/charge data later. 
        Utils.add_column(df, Const.PROTOCOL, np.nan)

        # Get the cycle metrics
        cycle_metrics = df[(df[Const.CHARGE_CYCLE_IDC]) | (df[Const.DISCHARGE_CYCLE_IDC])]

        for i in range(0, len(cycle_metrics)):
            t_start = cycle_metrics[Const.TIME].iloc[i]
            if i == len(cycle_metrics) - 1: # if last subcycle, end of subcycle = end of file 
                t_end = df[Const.TIME].iloc[-1]
            else: # end of subcycle = start of next subcycle
                t_end = cycle_metrics[Const.TIME].iloc[i+1]
            i_subcycle = df[Const.CURRENT][(t > t_start) & (t < t_end)]
            data_idx = cycle_metrics.index.tolist()[i]
            if is_cap_check:
                # hppc: ID by # of types of current sign changes (threshold is arbitrary)
                if len(np.where(np.diff(np.sign(i_subcycle)))[0]) > 10:
                    df.loc[data_idx, Const.PROTOCOL] = 'HPPC'
                # C/20 charge: longer than 8 hrs and mean(I)>0. Will ID C/10 during formation as C/20...
                elif (t_end - t_start) / 3600.0 > 8 and np.mean(i_subcycle) > 0 and np.mean(i_subcycle) < Const.QMAX / 18:
                    df.loc[data_idx, Const.PROTOCOL] = 'C/20 charge'
                # C/20 discharge: longer than 8 hrs and mean(I)<0. Will ID C/10 during formation as C/20...
                elif (t_end - t_start) / 3600.0 > 8 and np.mean(i_subcycle) < 0 and np.mean(i_subcycle) > - Const.QMAX / 18:
                    df.loc[data_idx, Const.PROTOCOL] = 'C/20 discharge'
            
        return df


    def find_cycle_idxs(self, t: pd.Series, i: pd.Series):
        """
        Find the cycle charge and discharge indices

        Parameters
        ----------
        t: pd.Series
            Time data
        i: pd.Series
            Current data

        Returns
        -------
        charge_start_idx: np.ndarray
            Array of indices where charging cycles start
        discharge_start_idx: np.ndarray
            Array of indices where discharging cycles start
        """

        ic = (i.values > 1e-5).astype(int)
        Id = (i.values < -1e-5).astype(int)
        potential_charge_start_idxs = np.where(np.diff(ic) > 0.5)[0]
        potential_discharge_start_idxs = np.where(np.diff(Id) > 0.5)[0]
        dt = np.diff(t)

        # Cumah = Ah_Charge - Ah_Discharge
        cumah = integrate.cumtrapz(i, t, initial=0) / 3600 / 1000  # ms to hours
        # Calculate the average discharge current and average time until the next charge step
        cumah = cumah - cumah.min()
        # Check for large gaps in the data, and reset the cumah counter.
        gap_index = np.argwhere(dt > 1e6)  # Look for gaps greater than 1000 seconds

        if gap_index.size > 0:
            if gap_index[0][0] > 0:
                for gap in gap_index[0]:
                    cumah[(gap + 1):] = cumah[gap] + cumah[(gap + 1):] - cumah[gap + 1]

        class_count = 10  # Basically needs to change by more than 10% of the full range.
        class_range = cumah.ptp()
        class_width = class_range / (class_count - 1)
        class_offset = cumah.min() - class_width / 2

        try:
            res = rfcnt.rfc(
                cumah,
                class_count=class_count,
                class_offset=class_offset,
                class_width=class_width,
                hysteresis=class_width,
                spread_damage=rfcnt.SDMethod.FULL_P2,  # Assign damage for closed cycles to 2nd turning point
                residual_method=rfcnt.ResidualMethod._NO_FINALIZE,  # Don't consider residues and leave internal sequence open
                wl={"sd": 1e3, "nd": 1e7, "k": 5}
            )

            turning_points = res["tp"][:, 0].astype(int) - 1
            cum_ah_at_turn = res["tp"][:, 1]
            last_tp = 0

            if len(turning_points) > 2:
                if cum_ah_at_turn[1] > cum_ah_at_turn[0]:
                    # 2nd turning point is the start of discharge.
                    charge_start_idx = np.array([min(potential_charge_start_idxs, key=lambda x: abs(x - turning_points[0]))])
                    discharge_start_idx = np.array([min(potential_discharge_start_idxs, key=lambda x: abs(x - turning_points[1]))])
                    last_tp = 1

                elif cum_ah_at_turn[0] - class_offset > class_range / 2:
                    # The first turning point is likely the start of discharge.
                    charge_start_idx = np.array([0])  # Case of a partial cycle, so set the charge start to the start of the file.
                    if turning_points[0] > charge_start_idx[0] - 10:  # Check that it comes after the first charge
                        discharge_start_idx = np.array([min(potential_discharge_start_idxs, key=lambda x: abs(x - turning_points[0]))])
                        last_tp = 0
                    else:
                        discharge_start_idx = np.array([min(potential_discharge_start_idxs, key=lambda x: abs(x - turning_points[1]))])
                        last_tp = 1
                        logger.debug("Choosing next turning point caveat emptor.")

                else:
                    charge_start_idx = np.array([potential_charge_start_idxs[0]])
                    if turning_points[1] > charge_start_idx[0] - 100:
                        discharge_start_idx = np.array([min(potential_discharge_start_idxs, key=lambda x: abs(x - turning_points[1]))])
                        last_tp = 1

                # Need to add the else case here in case we don't start with a charge cycle.

                for i in range(last_tp + 1, len(turning_points) - 1, 2):
                    charge_start_idx = np.append(charge_start_idx, min(potential_charge_start_idxs, key=lambda x: abs(x - turning_points[i])))
                    discharge_start_idx = np.append(discharge_start_idx, min(potential_discharge_start_idxs, key=lambda x: abs(x - turning_points[i + 1])))

            else:
                # No turning points in the data, just take the extents? This will probably break something else...
                charge_start_idx = []
                discharge_start_idx = []

        except Exception:
            logger.warning("No cycles detected (using the whole test).")
            charge_start_idx = []
            discharge_start_idx = []

        return charge_start_idx, discharge_start_idx

    def match_charge_discharge(self, charge_start_idxs: np.array, discharge_start_idxs: np.array):
        """
        Match charge and discharge start idxs for cycles starting with charge 
        and followed by discharge, considering inputs are numpy arrays.

        Parameters
        ----------
        charge_start_idxs : numpy.array
            Array of charge start idxs.
        discharge_start_idxs : numpy.array
            Array of discharge start idxs.

        Returns
        -------
        numpy.array
            Array of matched charge start idxs.
        numpy.array
            Array of matched discharge start idxs.
        """
        # Ensure cycling starts with a charge by adjusting the discharge idxs if necessary
        if discharge_start_idxs[0] < charge_start_idxs[0]:
            discharge_start_idxs = discharge_start_idxs[1:]
        
        # Match cycles based on the shortest sequence
        min_length = min(len(charge_start_idxs), len(discharge_start_idxs))
        matched_charge_idxs = charge_start_idxs[:min_length]
        matched_discharge_idxs = discharge_start_idxs[:min_length]

        return matched_charge_idxs, matched_discharge_idxs
    

    def calc_capacities(self, time_series: pd.Series, aht: pd.Series, charge_idxs: list, discharge_idxs: list, qmax: int):
        """
        Calculate the charge and discharge capacities based on Ah throughput data.

        Parameters
        ----------
        time_series : pd.Series
            Time data
        aht : pd.Series
            Ah throughput data
        charge_idxs : list
            Indices of charge cycles
        discharge_idxs : list    
            Indices of discharge cycles
        qmax : int
            Maximum capacity

        Returns
        -------
        tuple of np.ndarray
            The charge and discharge capacities.
        """
        cycle_idxs = sorted(charge_idxs + discharge_idxs + [len(time_series) - 1])
        
        charge_capacities = []
        discharge_capacities = []

        for i in range(len(cycle_idxs) - 1):
            capacity = aht[cycle_idxs[i + 1]] - aht[cycle_idxs[i]]
            
            if capacity > qmax:
                capacity = np.nan
                logger.warning(f"Invalid Capacity for cycle {i}")
            
            if cycle_idxs[i] in charge_idxs:
                charge_capacities.append(capacity)
            else:
                discharge_capacities.append(capacity)
        
        return np.array(charge_capacities), np.array(discharge_capacities)

    def calc_avg_cycle_data(self, time_series: np.array, data: np.array, charge_idxs: list, discharge_idxs: list):
        """
        Calculate average data (e.g., voltage, temperature, or expansion) for each cycle.

        Parameters:
        time_series : numpy array
            Time data
        data : numpy array
            Data to calculate average
        charge_idxs : list
            Indices of charge cycles
        discharge_idxs : list
            Indices of discharge cycles

        Returns:
        numpy array
            Averages of charge cycles
        numpy array
            Averages of discharge cycles
        """
        # Combine and sort idxs to mark the start of each cycle, including the last data point
        cycle_starts = sorted(charge_idxs + discharge_idxs + [len(time_series) - 1])

        averages_charge = []
        averages_discharge = []
        
        # Calculate the average for each cycle
        for i in range(len(cycle_starts) - 1):
            start_idx, end_idx = cycle_starts[i], cycle_starts[i + 1]
            time_delta = time_series[end_idx] - time_series[start_idx]
            
            if time_delta > 0:
                cycle_data = data[start_idx:end_idx]
                cycle_time = time_series[start_idx:end_idx]
                average = np.trapz(cycle_data, cycle_time) / time_delta
            else:
                average = 0
            
            # Assign the calculated average to the corresponding list based on cycle type
            if start_idx in charge_idxs:
                averages_charge.append(average)
            else:
                averages_discharge.append(average)
        
        return np.array(averages_charge), np.array(averages_discharge)

    #-----------------Shared Methods-----------------#

    def max_min_cycle_data(self, data: pd.Series, cycle_idxs_minmax: list):
        """
        Calculate the min and max data for each cycle.
        e.g. voltage, temperature, or expansion

        Parameters
        ----------
        data : pd.Series
            Data to calculate min and max
        cycle_idxs_minmax : list
            Cycle idxs

        Returns
        -------
        list
            Max data for each cycle
        list
            Min data for each cycle
        """
        max_vals  = []
        min_vals  = []

        for i in range(len(cycle_idxs_minmax) - 1):
            # If there's cycle data between two consecutive points, calculate max and min
            if len(data[cycle_idxs_minmax[i]:cycle_idxs_minmax[i + 1]]) > 0:
                max_vals.append(max(data[cycle_idxs_minmax[i]:cycle_idxs_minmax[i+1]]))
                min_vals.append(min(data[cycle_idxs_minmax[i]:cycle_idxs_minmax[i+1]]))
            # Handling edge cases
            else: 
                max_vals.append(data[cycle_idxs_minmax[i]])   
                min_vals.append(data[cycle_idxs_minmax[i]])  

        return max_vals, min_vals
    

#----------------- RPT Data Processing -----------------#
    
    def summarize_rpt_data(self, project_name: str = 'DEFAULT'):
        """
        Summarize the RPT data for each RPT test record.
        """
        # Get the RPT filenames, and create a dictionary to store the indices of each RPT file
        rpt_filename_to_idxs = {
            rpt_file: []
            for rpt_file in self.cell_cycle_metrics[Const.TEST_NAME][
                self.cell_cycle_metrics[Const.CYCLE_TYPE].isin(Const.RPT_TYPES)
            ].unique()
        }
        for idx, row in self.cell_cycle_metrics.iterrows():
            test_name = row[Const.TEST_NAME]
            if test_name in rpt_filename_to_idxs:
                rpt_filename_to_idxs[test_name].append(idx)

        cycle_summary_cols = [c for c in self.cell_cycle_metrics.columns if '[' in c] + [Const.TEST_NAME, Const.PROTOCOL]
        i_c20 = self.get_project_parameters(project_name)

        cell_rpt_data_list = []  # Use a list to collect dataframes to concatenate later

        for rpt_file, rpt_idxs in rpt_filename_to_idxs.items():
            # Mark as the previous rpt cycle
            pre_rpt_subcycle = pd.DataFrame()

            for i in rpt_idxs:
                t_start = self.cell_cycle_metrics.at[i, Const.TIME] - 30
                t_end = self.cell_data[Const.TIME].iloc[-1] + 30 if i + 1 >= len(self.cell_cycle_metrics) else self.cell_cycle_metrics.at[i + 1, Const.TIME] + 30
                
                rpt_subcycle = self.cell_cycle_metrics.loc[i, cycle_summary_cols].to_dict()
                rpt_subcycle[Const.RPT] = rpt_file
                t = self.cell_data[Const.TIME]
                rpt_subcycle[Const.DATA] = self.cell_data.loc[(t > t_start) & (t < t_end), 
                                        [Const.TIME, Const.CURRENT, Const.VOLTAGE, Const.AHT, Const.TEMPERATURE, Const.STEP_IDX]]

                self.update_cycle_metrics_hppc(rpt_subcycle, i)

                # Process ESOH data for the charge and discharge cycles
                if not pre_rpt_subcycle.empty:
                    protocols = {rpt_subcycle[Const.PROTOCOL], pre_rpt_subcycle[Const.PROTOCOL]}
                    if protocols == {'C/20 charge', 'C/20 discharge'}:
                        self.update_cycle_metrics_esoh(rpt_subcycle, pre_rpt_subcycle, i, i_c20)
                        pre_rpt_subcycle = pd.DataFrame()
                    
                t_vdf = self.cell_data_vdf[Const.TIME]
                if not t_vdf.empty:
                    rpt_subcycle[Const.DATA_VDF] = self.cell_data_vdf.loc[(t_vdf > t_start) & (t_vdf < t_end)]

                cell_rpt_data_list.append(pd.DataFrame([rpt_subcycle]))

        cell_rpt_data = pd.concat(cell_rpt_data_list, ignore_index=True)
        cell_rpt_data.reset_index(drop=True, inplace=True)

        cols = cell_rpt_data.columns.tolist()
        cols.insert(0, cols.pop(cols.index(Const.PROTOCOL)))
        cell_rpt_data = cell_rpt_data[cols]

        # Sort the DataFrame based on the first timestamp in 'Data' column
        cell_rpt_data = cell_rpt_data.sort_values(by=Const.DATA, key=lambda x: x[Const.TIME].iloc[0] if not x.empty else pd.NaT)
        
        self.cell_rpt_data = cell_rpt_data


    def get_project_parameters(self, project_name: str):
        pass

    def update_cycle_metrics_hppc(self, rpt_subcycle: dict, i: int):
        """
        Update the cell_cycle_metrics with HPPC data.

        Parameters
        ----------
        rpt_subcycle : dict
            Subcycle data
        i : int
            Index of the subcycle in cell_cycle_metrics
        """
        if rpt_subcycle[Const.PROTOCOL] == 'HPPC':
            # Extract necessary data for get_Rs_SOC function
            time_ms = rpt_subcycle[Const.DATA][0][Const.TIME] / 1000.0
            current = rpt_subcycle[Const.DATA][0][Const.CURRENT]
            voltage = rpt_subcycle[Const.DATA][0][Const.VOLTAGE]
            ah_throughput = rpt_subcycle[Const.DATA][0][Const.AHT]
            
            hppc_data = self.get_rs_soc(time_ms, current, voltage, ah_throughput)

            # Dynamically generate metrics_mapping based on PULSE_CURRENTS
            for col in Const.CCM_COLUMNS_ADDITIONAL_HPPC:
                if col not in self.cell_cycle_metrics.columns:
                    self.cell_cycle_metrics[col] = np.nan
                self.cell_cycle_metrics[col] = self.cell_cycle_metrics[col].astype(object)
                # Update the cell_cycle_metrics with the new data
                if col in hppc_data:
                    self.cell_cycle_metrics.at[i, col] = hppc_data[col].tolist()


    def update_cycle_metrics_esoh(self, rpt_subcycle: dict, pre_rpt_subcycle: dict, i: int, i_slow: float):
        """
        Calculate eSOH for the charge and discharge cycles and update the cell_cycle_metrics.

        Parameters
        ----------
        rpt_subcycle : dict
            Subcycle data
        pre_rpt_subcycle : dict
            Previous subcycle data
        i : int
            Index of the subcycle in cell_cycle_metrics
        i_slow : float
            The RPT dis/charge current magnitude (e.g. C/20) [A]
        """
        # Skip the Formation data
        if '_F_' in rpt_subcycle[Const.RPT]:
            return        

        if pre_rpt_subcycle[Const.PROTOCOL] == 'C/20 charge':
            dh_subcycle = rpt_subcycle
            ch_subcycle = pre_rpt_subcycle
        else:
            dh_subcycle = pre_rpt_subcycle
            ch_subcycle = rpt_subcycle
        try:
            logger.info(f"Processing eSOH for {rpt_subcycle[Const.TEST_NAME]}")
            q_data, v_data, dvdq_data, q_full = self.load_v_data(ch_subcycle, dh_subcycle, i_slow)
            theta, cap, err_v, err_dvdq, p1_err, p2_err, p12_err = self.esoh_est(q_data, v_data, dvdq_data, q_full)
            if err_v > 20:
                logger.warning(f"Error in V estimation is too high: {err_v}.")
                theta[:] = np.NaN

        except Exception as e:
            theta, cap, err_v, err_dvdq, p1_err, p2_err, p12_err = np.full(6, np.NaN), np.NaN, np.NaN, np.NaN, np.NaN, np.NaN, np.NaN
            logger.error(f"Error while processing eSOH: {e}") # ERROR: can only assign an iterable

        # Update the cell_cycle_metrics with eSOH data
        esoh_vals = [cap, *theta, err_v, err_dvdq, p1_err, p2_err, p12_err]
        for j, col in enumerate(Const.CCM_COLUMNS_ADDITIONAL_ESOH):
            self.cell_cycle_metrics.at[i, col] = esoh_vals[j]


    def load_v_data(self, ch_rpt: dict, dh_rpt: dict, i_slow: float, i_threshold: float = 0.005, d_int: float =0.01): 
        """
        Method used for eSOH calculation
        """
        d_ch = ch_rpt[Const.DATA][0]
        d_dh = dh_rpt[Const.DATA][0]
        d_dh = d_dh.reset_index(drop=True)
        d_ch = d_ch.reset_index(drop=True)
        d_dh[Const.TIME] = d_dh[Const.TIME] - d_dh[Const.TIME].iloc[0]
        d_ch[Const.TIME] = d_ch[Const.TIME] - d_ch[Const.TIME].iloc[0]
        d_ch = d_ch[d_ch[Const.TIME] <= 1e8]
        d_dh = d_dh[d_dh[Const.TIME] <= 1e8]
        d_ch1 = d_ch[(d_ch[Const.CURRENT] > 0)] # charge current is + 
        d_dh1 = d_dh[(d_dh[Const.CURRENT] < 0)] # discharge current is - 
        d_ch = d_ch[(d_ch[Const.CURRENT] > (i_slow - i_threshold)) & (d_ch[Const.CURRENT] < (i_slow + i_threshold))]
        d_dh = d_dh[(d_dh[Const.CURRENT] < -(i_slow - i_threshold)) & (d_dh[Const.CURRENT] > -(i_slow + i_threshold))]
        q_cv = max(d_ch1[Const.AHT]) - max(d_ch[Const.AHT])
        
        # Filter data points with only V>2.7 V and V< 4.2V
        d_ch = d_ch[(d_ch[Const.VOLTAGE] >= 2.7) & (d_ch[Const.VOLTAGE] <= 4.2)]
        d_dh = d_dh[(d_dh[Const.VOLTAGE] >= 2.7) & (d_dh[Const.VOLTAGE] <= 4.2)]
        d_ch[Const.AHT] = d_ch[Const.AHT] - d_ch[Const.AHT].iloc[0]
        d_dh[Const.AHT] = d_dh[Const.AHT] - d_dh[Const.AHT].iloc[0]

        t_d = d_dh[Const.TIME].to_numpy()
        t_d = t_d - t_d[0]
        t_d = t_d / 1e3 
        i_d = d_dh[Const.CURRENT].to_numpy()
        v_d = d_dh[Const.VOLTAGE].to_numpy()
        q_d = integrate.cumtrapz(abs(i_d), t_d/3600)
        q_d = np.append(q_d,q_d[-1])
        t_c = d_ch[Const.TIME].to_numpy()
        t_c = t_c - t_c[0]
        t_c = t_c/1e3
        i_c = d_ch[Const.CURRENT].to_numpy()
        v_c = d_ch[Const.VOLTAGE].to_numpy()
        ah_c = d_ch[Const.AHT].to_numpy()
        q_c = integrate.cumtrapz(abs(i_c), t_c/3600)
        q_c = np.append(q_c, q_c[-1])

        ## Normalizing from SOC=100
        ah_c = ah_c[-1] - ah_c + q_cv
        q_c = q_c[-1] - q_c + q_cv

        # Interpolate for Averaging
        q_d,idx_d = np.unique(q_d,return_index=True)
        v_d = v_d[idx_d]
        q_c,idx_c = np.unique(q_c,return_index=True)
        v_c = v_c[idx_c]

        dt = np.average(np.diff(t_d))
        dt = round(dt,1)

        window = int(3000/dt + 1)
        qf_d, vf_d, dvdq_d = self.filter_qv_data(q_d, v_d, window)
        qf_c, vf_c, dvdq_c = self.filter_qv_data(q_c, v_c, window)

        qf_d, idx_d = np.unique(qf_d, return_index=True)
        vf_d = vf_d[idx_d]
        qf_c,idx_c = np.unique(qf_c, return_index=True)
        vf_c = vf_c[idx_c]
        int_v_d = interpolate.CubicSpline(qf_d, vf_d, extrapolate=True)
        int_dvdq_d = interpolate.CubicSpline(qf_d, dvdq_d, extrapolate=True)
        int_v_c = interpolate.CubicSpline(qf_c, vf_c, extrapolate=True)
        int_dvdq_c = interpolate.CubicSpline(qf_c, dvdq_c, extrapolate=True)

        qmax = np.min([np.max(qf_d), np.max(qf_c)])
        qmin = np.max([np.min(qf_d), np.min(qf_c)])
        qfull = np.max([np.max(qf_d), np.max(qf_c)])

        qin = np.arange(qmin, qmax, d_int)
        v_d_int = int_v_d(qin)
        v_c_int = int_v_c(qin)
        dvdq_d_int = int_dvdq_d(qin)
        dvdq_c_int = int_dvdq_c(qin)
        v_avg = (v_d_int + v_c_int) / 2
        dvdq_avg = (dvdq_d_int + dvdq_c_int) / 2

        return qin, v_avg, dvdq_avg, qfull

    def esoh_est(self, q_data: np.array, v_data: np.array, dvdq_data: np.array, q_full: float, window: int = 5):
        """
        Method used for eSOH calculation
        """
        cap = q_full
        bounds = Bounds(Const.LB, Const.UB)
        nleq1 = lambda x: -cap / x[0] + x[1]
        nlcon1 = NonlinearConstraint(nleq1, 0, 1)
        nleq2 = lambda x: cap / x[2] + x[3]
        nlcon2 = NonlinearConstraint(nleq2, 0, 1)
        result = minimize(self.fitfunc, Const.X0, args = (q_data, v_data, dvdq_data), 
                          bounds = bounds, constraints = [nlcon1, nlcon2])
        res = result.x
        cn = res[0]
        cp = res[2]
        x100 = res[1]
        y100 = res[3]
        x0 = res[1] - cap / res[0]
        y0 = res[3] + cap / res[2]
        theta = [cn, x0, x100, cp, y0, y100]
        theta = [round(tt, 4) for tt in theta]
        v_fit = np.concatenate([[self.calc_opc(res, q)] for q in q_data])

        err_V = 1e3 * np.linalg.norm(v_data - v_fit) / np.sqrt(len(v_data))
        err_V = round(err_V,1)
        cap = round(cap,3)
        _, _, dvdq_fit = self.filter_qv_data(q_data, v_fit, window)
        q1_data, q2_data = self.get_peaks(q_data, dvdq_data)
        q1_fit, q2_fit = self.get_peaks(q_data, dvdq_fit)
        p1_err = q1_data - q1_fit
        p2_err = q2_data - q2_fit
        p12_data = q2_data - q1_data
        p12_fit = q2_fit - q1_fit
        p12_err = p12_data - p12_fit
        q_bool = (q_data > 0.1 * cap) & (q_data < 0.9 * cap)
        dVdQ_data_f = dvdq_data[q_bool]
        dVdQ_fit_f = dvdq_fit[q_bool]
        q_f = q_data[q_bool]
        
        err_dVdQ = 1e3*np.linalg.norm(dVdQ_data_f-dVdQ_fit_f) / np.sqrt(len(v_data))
        err_dVdQ = round(err_dVdQ, 1)

        return theta, cap, err_V, err_dVdQ, p1_err, p2_err, p12_err
    

    def filter_qv_data(self, qdata: np.array, vdata: np.array, window_length: int, polyorder: int = 3):
        """
        Filter the QV data using a Savitzky-Golay filter.

        Parameters
        ----------
        qdata : np.array
            Q data
        vdata : np.array
            V data
        window_length : int
            Length of the filter window
        polyorder : int, optional
            Order of the polynomial

        Returns
        -------
        np.array
            Filtered Q data
        np.array
            Filtered V data
        np.array
            Filtered dVdQ data
        """
        qf = savgol_filter(qdata, window_length, polyorder, 0)
        dq = -savgol_filter(qdata, window_length, polyorder, 1)
        vf = savgol_filter(vdata, window_length, polyorder, 0)
        dv = savgol_filter(vdata, window_length, polyorder, 1)
        dvdq = dv / dq
        return qf, vf, dvdq

    def get_rs_soc(self, t: np.array, i: np.array, v: np.array, q: np.array):
        """ 
        Processes HPPC data to get DC Resistance for given pulse currents at different Qs. 
        Assumes that this is a discharge HPPC i.e. the initial Q is 0, correspondign to 100% SOC

        Parameters
        ----------
        t : np.array
            Time data
        i : np.array
            Current data
        v : np.array
            Voltage data
        q : np.array
            Ah throughput data

        Returns
        -------
        pd.DataFrame
            Processed HPPC data
        """
        pts = 4
        diff_i = np.diff(i)
        idxi1 = np.where((diff_i > 0.1) & (i[1:] > 0.1))[0]
        idxi2 = np.where((diff_i < -0.1) & (i[1:] < -0.1))[0]
        idxi = np.sort(np.concatenate([idxi1, idxi2])) + 1

        idxk1 = np.where((diff_i < -0.1) & (i[:-1] > 0.1))[0]
        idxk2 = np.where((diff_i > 0.1) & (i[:-1] < -0.1))[0]
        idxk = np.sort(np.concatenate([idxk1, idxk2]))

        no_pulses = min(len(idxi), len(idxk))

        records = []
        for pno in range(no_pulses):
            if idxk[pno] + 1 - pts < 0 or idxi[pno] - 1 - pts < 0:  # Ensure index bounds
                continue
            t1, v1, i1 = t[idxi[pno] - 1 - pts:idxi[pno] - 1], v[idxi[pno] - 1 - pts:idxi[pno] - 1], i[idxi[pno] - 1 - pts:idxi[pno] - 1]
            t2, v2, i2 = t[idxi[pno]:idxi[pno] + pts], v[idxi[pno]:idxi[pno] + pts], i[idxi[pno]:idxi[pno] + pts]
            t3, v3, i3 = t[idxk[pno] + 1 - pts:idxk[pno] + 1], v[idxk[pno] + 1 - pts:idxk[pno] + 1], i[idxk[pno] + 1 - pts:idxk[pno] + 1]
            
            if len(t1) < pts or len(t2) < pts or len(t3) < pts:  # Ensure sufficient data points
                continue

            r_p1 = np.abs((np.mean(v2) - np.mean(v1)) / (np.mean(i2) - np.mean(i1)))
            r_p2 = np.abs((np.mean(v3) - np.mean(v1)) / (np.mean(i3) - np.mean(i1)))
            q_val = np.mean(q[idxi[pno] - 1 - pts:idxi[pno] - 1])

            records.append({
                Const.PULSE_CURRENT: round(np.mean(i2), 3),
                Const.PULSE_DURATION: round(np.mean(t3) - np.mean(t1), 3),
                Const.PULSE_Q: q_val,
                Const.R_S: round(r_p1, 4),
                Const.R_L: round(r_p2, 4)
            })

        return pd.DataFrame(records)

    def fitfunc(self, x: np.array, q_data: np.array, v_data: np.array, dvdq_data: np.array, dvdq_bool: bool = True):
        """
        Method used for scipy.optimize.minimize in eSOH calculation.
        """
        model = np.concatenate([
                [self.calc_opc(x, q)]
                for q in q_data
            ]
        )
        qa, qb = self.get_peaks(q_data, dvdq_data)
        q_max = np.max(q_data)
        # Q1 = 0*0.1*Qmax
        q1 = 0.1 * q_max
        q2 = 0.9 * q_max
        if qa>0 and qa<0.5 * q_max and dvdq_bool:
            q3 = qa - 0.05 * q_max
            q4 = qa + 0.05 * q_max
        else:
            q3 = 0.25 * q_max
            q4 = 0.45 * q_max
        if qb < q_max and qb > 0.5 * q_max and dvdq_bool:
            q5 = qb - 0.05 * q_max
            q6 = qb + 0.05 * q_max
        else:
            q5 = 0.7 * q_max
            q6 = 0.9 * q_max
        wvec = np.ones(len(q_data))
        for i,q in enumerate(q_data):
            if q < q1 or q > q2:
                wvec[i] = Const.W1
            if q >= q1 and q <= q2:
                wvec[i] = Const.W2
            if q >= q3 and q <= q4:
                wvec[i] = Const.W3
            if q >= q5 and q <= q6:
                wvec[i] = Const.W3
        vd = np.multiply(v_data, wvec)
        vs = np.multiply(model, wvec)
        error = vd - vs
        out = np.linalg.norm(error) / np.sqrt(len(vd))
        return out
    
    def get_peaks(self, q: np.array, dvdq: np.array):
        """
        Find the peaks in the dVdQ data. Method used for eSOH calculation.
        """
        q_max = max(q)
        pks, _ = find_peaks(dvdq,prominence=0.01)
        pks = [pk for pk in pks if q[pk] >= 0.1 * q_max and q[pk] <= 0.9 * q_max ]
        q_peaks = q[pks]
        if len(q_peaks) > 0:
            q1 = q_peaks[0]
            q2 = q_peaks[-1]
        else:
            q1 = q[0]
            q2 = q[-1]
        return q1, q2
    
    def calc_opc(self, x: np.array, q: float):
        """
        Calculate the open circuit potential (OPC) for a given stoichiometry.
        Method used for eSOH calculation.
        """
        
        # Calculate UN
        sto_un = x[1] - q / x[0]
        p_eq = Const.UN_VAR1[0:8]
        a_eq = Const.UN_VAR1[8:15]
        b_eq = Const.UN_VAR1[15:]
        u_eq2 = p_eq[-1] + p_eq[-2] * np.exp((sto_un - a_eq[-1]) / b_eq[-1])
        for i in range(6):
            u_eq2 += p_eq[i] * np.tanh((sto_un - a_eq[i]) / b_eq[i])
        p_eq = Const.UN_VAR2[0:8]
        a_eq = Const.UN_VAR2[8:15]
        b_eq = Const.UN_VAR2[15:]
        u_eq1 = p_eq[-1] + p_eq[-2] * np.exp((sto_un - a_eq[-1]) / b_eq[-1])
        for i in range(6):
            u_eq1 += p_eq[i] * np.tanh((sto_un - a_eq[i]) / b_eq[i])
        un = (u_eq1 + u_eq2) / 2
        
        # Calculate UP
        sto_up = x[3] + q / x[2]
        up = (Const.P1 * (sto_up ** 9) + Const.P2 * (sto_up ** 8) + Const.P3 * (sto_up ** 7) + 
              Const.P4 * (sto_up ** 6) + Const.P5 * (sto_up ** 5) + Const.P6 * (sto_up ** 4) + 
              Const.P7 * (sto_up ** 3) + Const.P8 * (sto_up ** 2) + Const.P9 * sto_up + Const.P10)
        
        # Calculate OPC
        ocp = up - un
        
        return ocp